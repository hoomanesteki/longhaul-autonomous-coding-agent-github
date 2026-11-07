"""The stuck detector: four rules over a short window of per-turn signals (LH-505).

A coder that goes in circles costs money and tells nobody. This module notices four ways of
going in circles, from facts that are cheap to keep:

- NO_PROGRESS: in the last `no_progress_turns` turns no test run passed more tests than any run
  before it, and no plan step was finished. The first test count the detector sees counts as a
  gain, because the agent has learned something it did not know. The steps done so far count as
  a gain on the first turn of a window in the same way, so a step finished on that turn is
  never missed. A window that starts again after a signal can be one turn late for it, and
  never early.
- REPEATED_FAILURE: the same command failed the same way `repeated_failure_count` times. Turns
  that ran no failing command do not break the streak, because an agent edits between two runs
  of its tests. A different failure does.
- EDIT_CHURN: one region of a file was edited `churn_edits` times in the last 10 turns and the
  count of passing tests did not move. Two regions are the same when they share half or more of
  the lines of the longer one. A small fix inside a big region is not the same region: a
  `write_file` covers its whole file, and three fixes in three places of a new file are the
  normal way to build it.
- TOOL_ERRORS: the last `consecutive_tool_errors` tool calls all failed. A turn with one good
  call ends the run of errors, even if its other calls failed.

A turn can match more than one rule. The detector reports the most specific one: a failure with
a name is what a person can act on, and "no progress" is what is left when none fits (RULES).

The module is pure: no I/O, no clock, no random, and it imports only the contracts. TaskWorkflow
imports it (invariant I1) and keeps the window in `CarryState.stuck_window`. The facts come from
the activities. `tool_exec` works out a `CallSignal` for each call that edits or runs something
(durable/activities/signals.py), and `read_turn` folds the signals of a turn, with the results of
its calls, into one `TurnSignal`.

The window is bounded by construction. It holds the last 30 turns, and the edited regions of a
turn older than the last 10 are dropped, since only EDIT_CHURN reads them and it looks 10 turns
back. The text for the evidence (`TurnNotes`) is not kept at all: a rule fires on the turn it
finishes on, and the notes of that turn are all the evidence needs. `tests/unit/test_stuck.py`
feeds the detector the largest signals it can be given and checks that the window stays under
8 KB.

A signal that fires empties the window. Without that the same pattern would fire again on the next
turn, and the one after, until the agent broke it. The pause and hint flow (LH-506) acts on the
signal and starts from a clean window, and a pattern that goes on after it builds up again from
nothing. The detector watches the coder only: it ignores everything it is given in the planning
phase, where reading the same file three times is the job.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from longhaul.contracts import Phase, StuckReason, StuckSignal, ToolResult, TurnSignal

# The turns the window holds, and how many of the latest ones EDIT_CHURN reads (plan LH-505).
WINDOW_TURNS = 30
CHURN_TURNS = 10

# What one turn keeps of its edits and what a note keeps of a text, so that the window is small
# whatever the agent does. A path is cut from the front: its end names the file. The limit on a
# path is in bytes of JSON, the form the state is stored in, since the limit on the state is in
# bytes too and a file name can be in any script.
MAX_REGIONS_PER_TURN = 3
MAX_PATH_BYTES = 48
MAX_TEXT_CHARS = 80

Region = tuple[str, int, int]  # path, first line, last line


# ---------- the limits ----------
class StuckLimits(Protocol):
    """The four thresholds. The `stuck` section of the config (settings.Stuck) has them."""

    @property
    def no_progress_turns(self) -> int: ...

    @property
    def repeated_failure_count(self) -> int: ...

    @property
    def churn_edits(self) -> int: ...

    @property
    def consecutive_tool_errors(self) -> int: ...


@dataclass(frozen=True)
class Limits:
    no_progress_turns: int
    repeated_failure_count: int
    churn_edits: int
    consecutive_tool_errors: int


# The workflow cannot read the config file, so these are the numbers of `stuck` in
# config/longhaul.example.yaml. A unit test keeps the two equal, and a worker whose config has
# other numbers stops at the start (durable/worker.py), as it does for the planner's turns.
DEFAULT_LIMITS = Limits(
    no_progress_turns=15,
    repeated_failure_count=3,
    churn_edits=4,
    consecutive_tool_errors=5,
)


def limits_of(cfg: StuckLimits) -> Limits:
    return Limits(
        no_progress_turns=cfg.no_progress_turns,
        repeated_failure_count=cfg.repeated_failure_count,
        churn_edits=cfg.churn_edits,
        consecutive_tool_errors=cfg.consecutive_tool_errors,
    )


# ---------- the facts of a call, and of a turn ----------
class CallSignal(BaseModel):
    """What tool_exec learned from one call, for the detector. Every field has a small bound.

    `failed_command` and `failed_error` are text for the evidence, cut to MAX_TEXT_CHARS. Only
    `failed_cmd_hash` takes part in a rule.
    """

    tests_passed: int | None = None
    failed_cmd_hash: str | None = None
    failed_command: str | None = None
    failed_error: str | None = None
    edited_regions: list[Region] = Field(default_factory=list)


@dataclass(frozen=True)
class TurnNotes:
    """Short texts of the latest turn, for the evidence. The window does not keep them."""

    failed_command: str | None = None
    failed_error: str | None = None
    tool_error: str | None = None


def short(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    """The text on one line, cut to `limit` characters."""
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3].rstrip() + "..."


def _json_bytes(char: str) -> int:
    """The bytes that one character of a string takes in the JSON of the state."""
    code = ord(char)
    if char in '"\\':
        return 2
    if code < 0x20:
        return 6  # most control characters are written as \u00xx
    if code < 0x80:
        return 1
    return 2 if code < 0x800 else 3 if code < 0x10000 else 4


def short_path(path: str) -> str:
    """The path, or its end behind "..." when it takes more than MAX_PATH_BYTES of JSON."""
    if sum(_json_bytes(char) for char in path) <= MAX_PATH_BYTES:
        return path
    room = MAX_PATH_BYTES - len("...")
    kept: list[str] = []
    for char in reversed(path):
        room -= _json_bytes(char)
        if room < 0:
            break
        kept.append(char)
    return "..." + "".join(reversed(kept))


def read_turn(
    index: int,
    steps_done: int,
    facts: Sequence[CallSignal],
    results: Sequence[ToolResult],
    names: Mapping[str, str],
) -> tuple[TurnSignal, TurnNotes]:
    """Fold the facts and the results of one coder turn into its signal and its notes.

    `facts` are the signals of the calls that tool_exec ran, in order, `results` are the results
    of every call of the turn (the control calls included), and `names` maps a call id to the
    name of its tool. The last test count and the last failed command of the turn stand for it.
    """
    counts = [f.tests_passed for f in facts if f.tests_passed is not None]
    failed = [f for f in facts if f.failed_cmd_hash is not None]
    regions = [(short_path(p), a, b) for f in facts for p, a, b in f.edited_regions]
    errors = [r for r in results if not r.ok]
    last = failed[-1] if failed else None
    signal = TurnSignal(
        turn_index=index,
        tests_passed=counts[-1] if counts else None,
        steps_done=steps_done,
        failed_cmd_hash=last.failed_cmd_hash if last else None,
        edited_regions=regions[:MAX_REGIONS_PER_TURN],
        tool_errors=len(errors),
        tool_calls=len(results),
    )
    notes = TurnNotes(
        failed_command=_short_or_none(last.failed_command if last else None),
        failed_error=_short_or_none(last.failed_error if last else None),
        tool_error=_tool_error(errors[-1], names) if errors else None,
    )
    return signal, notes


def _short_or_none(text: str | None) -> str | None:
    return short(text) if text else None


def _tool_error(result: ToolResult, names: Mapping[str, str]) -> str:
    name = names.get(result.call_id, "a tool")
    lines = result.output.strip().splitlines()
    return short(f"{name}: {lines[0]}" if lines else name)


# ---------- the rules ----------
# A rule gets the window, the limits and the notes of the latest turn, and returns the evidence
# when it fires.
Rule = Callable[[list[TurnSignal], Limits, TurnNotes], list[str] | None]


def _repeated_failure(
    window: list[TurnSignal], limits: Limits, notes: TurnNotes
) -> list[str] | None:
    failed = [s.failed_cmd_hash for s in window if s.failed_cmd_hash is not None]
    count = limits.repeated_failure_count
    if len(failed) < count or len(set(failed[-count:])) != 1:
        return None
    streak = 0
    for found in reversed(failed):
        if found != failed[-1]:
            break
        streak += 1
    what = notes.failed_command or "a command"
    same = notes.failed_error or "output"
    return [f"{what} failed {streak} times with the same {same}"]


def _tool_errors(window: list[TurnSignal], limits: Limits, notes: TurnNotes) -> list[str] | None:
    run = 0
    for turn in reversed(window):
        # A turn with a good call in it, or with no call, ends the run.
        if turn.tool_errors == 0 or turn.tool_errors < turn.tool_calls:
            break
        run += turn.tool_errors
    if run < limits.consecutive_tool_errors:
        return None
    evidence = [f"the last {run} tool calls failed in a row"]
    if notes.tool_error:
        evidence.append(f"last error: {notes.tool_error}")
    return evidence


def _same_region(a: Region, b: Region) -> bool:
    """True when the two edits share half or more of the lines of the longer one.

    The longer one sets the measure so that a region which covers a whole file (a write_file)
    does not take in every small edit of that file: two edits that both overlap it need not
    overlap each other.
    """
    if a[0] != b[0]:
        return False
    shared = min(a[2], b[2]) - max(a[1], b[1]) + 1
    longer = max(a[2] - a[1], b[2] - b[1]) + 1
    return shared > 0 and shared * 2 >= longer


def _tests_unchanged(window: list[TurnSignal]) -> bool:
    """True when every test count of the last turns, and the count before them, is one number."""
    counts = [s.tests_passed for s in window[-CHURN_TURNS:] if s.tests_passed is not None]
    earlier = [s.tests_passed for s in window[:-CHURN_TURNS] if s.tests_passed is not None]
    if earlier:
        counts.insert(0, earlier[-1])
    return len(set(counts)) <= 1


def _edit_churn(window: list[TurnSignal], limits: Limits, notes: TurnNotes) -> list[str] | None:
    edits = [region for turn in window[-CHURN_TURNS:] for region in turn.edited_regions]
    if len(edits) < limits.churn_edits or not _tests_unchanged(window):
        return None
    # The region that most edits share with. Each edit is tried as the anchor, so the answer
    # does not depend on which one came first.
    same: list[Region] = []
    for anchor in edits:
        shared = [edit for edit in edits if _same_region(anchor, edit)]
        if len(shared) > len(same):
            same = shared
    if len(same) < limits.churn_edits:
        return None
    first, last = min(e[1] for e in same), max(e[2] for e in same)
    where = f"line {first}" if first == last else f"lines {first}-{last}"
    return [
        f"{same[0][0]} {where} edited {len(same)} times in the last {CHURN_TURNS} turns "
        "with no test change"
    ]


def _progress(window: list[TurnSignal]) -> list[bool]:
    """For each turn, whether it passed more tests than any turn before it or finished a step."""
    best: int | None = None
    # A plan starts with no step done, and the window does not say how many were done before its
    # first turn. Counting from 0 means that a step finished on the first turn is a gain, as the
    # first test count is. A window that starts again after a signal can count a step that was
    # done before it, and is then one turn late for NO_PROGRESS, never early.
    steps = 0
    gained: list[bool] = []
    for turn in window:
        more_tests = turn.tests_passed is not None and (best is None or turn.tests_passed > best)
        if turn.tests_passed is not None:
            best = turn.tests_passed if best is None else max(best, turn.tests_passed)
        more_steps = turn.steps_done > steps
        steps = turn.steps_done
        gained.append(more_tests or more_steps)
    return gained


def _no_progress(window: list[TurnSignal], limits: Limits, notes: TurnNotes) -> list[str] | None:
    turns = limits.no_progress_turns
    if len(window) < turns or any(_progress(window)[-turns:]):
        return None
    counts = [s.tests_passed for s in window if s.tests_passed is not None]
    tests = str(counts[-1]) if counts else "none run"
    return [
        f"no new passing test and no finished step in the last {turns} turns",
        f"tests passed: {tests}, steps done: {window[-1].steps_done}",
    ]


# The most specific rule first.
RULES: tuple[tuple[StuckReason, Rule], ...] = (
    (StuckReason.REPEATED_FAILURE, _repeated_failure),
    (StuckReason.TOOL_ERRORS, _tool_errors),
    (StuckReason.EDIT_CHURN, _edit_churn),
    (StuckReason.NO_PROGRESS, _no_progress),
)


# ---------- the detector ----------
def _kept(window: list[TurnSignal]) -> list[TurnSignal]:
    """The last WINDOW_TURNS turns, without the edited regions that no rule can read any more."""
    window = window[-WINDOW_TURNS:]
    older = len(window) - CHURN_TURNS
    return [
        turn.model_copy(update={"edited_regions": []})
        if i < older and turn.edited_regions
        else turn
        for i, turn in enumerate(window)
    ]


class StuckDetector:
    """Watches the turns of the coder, one `observe` at a time.

    It holds a window and nothing else, so a workflow builds one from `CarryState.stuck_window`
    for each turn and writes `window` back. `cfg` is anything with the four limits, such as the
    `stuck` section of the config.
    """

    def __init__(self, cfg: StuckLimits, window: Iterable[TurnSignal] = ()) -> None:
        self._limits = limits_of(cfg)
        self._window = _kept(list(window))

    @property
    def window(self) -> list[TurnSignal]:
        return list(self._window)

    def observe(
        self,
        sig: TurnSignal,
        notes: TurnNotes | None = None,
        *,
        phase: Phase = Phase.CODING,
    ) -> StuckSignal | None:
        """Add a turn and say whether it completes a pattern. None while planning.

        `notes` are the short texts of this turn, for the evidence. A signal that fires empties
        the window (see the module docstring).
        """
        if phase is Phase.PLANNING:
            return None
        self._window = _kept([*self._window, sig])
        for reason, rule in RULES:
            evidence = rule(self._window, self._limits, notes or TurnNotes())
            if evidence is not None:
                self._window = []
                return StuckSignal(reason=reason, evidence=evidence)
        return None
