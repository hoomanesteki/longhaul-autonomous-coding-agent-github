"""The stuck detector: four rules over a bounded window of per-turn signals (LH-505).

The detector is pure Python, so every rule is tested here with plain signals and no workflow.
tests/integration/test_stuck_detector.py runs it inside TaskWorkflow. Each rule has a positive
and a negative test, and the negative ones are the near misses a false alarm would come from.
"""

from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from longhaul.contracts import Phase, StuckReason, StuckSignal, ToolResult, TurnSignal
from longhaul.roles import stuck
from longhaul.roles.stuck import CallSignal, StuckDetector, TurnNotes
from longhaul.settings import load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
LIMITS = stuck.DEFAULT_LIMITS
HASH_A = "a" * 40
HASH_B = "b" * 40
FILE = "src/inv.py"


def sig(turn: int, **fields: Any) -> TurnSignal:
    """A turn that ran one tool call, did nothing wrong and moved nothing, unless told."""
    fields.setdefault("tool_calls", 1)
    return TurnSignal(turn_index=turn, **fields)


def feed(
    signals: list[TurnSignal],
    notes: TurnNotes | None = None,
    *,
    detector: StuckDetector | None = None,
) -> list[StuckSignal | None]:
    """What the detector says after each signal, one answer per signal."""
    detector = detector or StuckDetector(LIMITS)
    return [detector.observe(s, notes) for s in signals]


def only_last(answers: list[StuckSignal | None]) -> StuckSignal:
    """The one signal that fired, which has to be the answer to the last turn."""
    assert answers[:-1] == [None] * (len(answers) - 1), "fired before the last turn"
    last = answers[-1]
    assert last is not None, "did not fire on the last turn"
    return last


# ---------- NO_PROGRESS ----------
@pytest.mark.parametrize(
    ("turns", "fires"),
    [
        # The first test count is news, so it counts as progress. The 15 turns after it that
        # change nothing are what the rule is after, and the last of them is the 16th signal.
        pytest.param([sig(i, tests_passed=4) for i in range(16)], True, id="pos"),
        # The same turns, except that the tests run on turn 9 pass one more than before.
        pytest.param([sig(i, tests_passed=5 if i >= 9 else 4) for i in range(16)], False, id="neg"),
    ],
)
def test_no_progress(turns: list[TurnSignal], fires: bool) -> None:
    answers = feed(turns)
    if not fires:
        assert answers == [None] * len(turns)
        return
    found = only_last(answers)
    assert found.reason is StuckReason.NO_PROGRESS
    assert "15 turns" in found.evidence[0]
    assert "tests passed: 4" in found.evidence[1]


def test_no_progress_counts_turns_even_when_no_test_ever_ran() -> None:
    found = only_last(feed([sig(i) for i in range(15)]))
    assert found.reason is StuckReason.NO_PROGRESS
    assert "tests passed: none run" in found.evidence[1]


def test_a_finished_step_is_progress() -> None:
    turns = [sig(i, tests_passed=4, steps_done=1 if i >= 8 else 0) for i in range(16)]
    assert feed(turns) == [None] * 16


def test_a_fall_in_passing_tests_is_not_progress() -> None:
    # 5 passed, then 3, then back to 5: the second 5 is not above the best, so it is no gain.
    turns = [sig(0, tests_passed=5), sig(1, tests_passed=3)]
    turns += [sig(i, tests_passed=5) for i in range(2, 17)]
    assert only_last(feed(turns)).reason is StuckReason.NO_PROGRESS


def test_fewer_turns_than_the_limit_never_fire() -> None:
    assert feed([sig(i) for i in range(14)]) == [None] * 14


# ---------- REPEATED_FAILURE ----------
FAILED = TurnNotes(failed_command="pytest tests/test_inv.py", failed_error="KeyError at inv.py:42")


@pytest.mark.parametrize(
    ("hashes", "fires"),
    [
        pytest.param([HASH_A, HASH_A, HASH_A], True, id="pos"),
        pytest.param([HASH_A, HASH_B, HASH_A], False, id="neg"),
    ],
)
def test_repeated_failure(hashes: list[str], fires: bool) -> None:
    turns = [sig(i, failed_cmd_hash=h) for i, h in enumerate(hashes)]
    answers = feed(turns, FAILED)
    if not fires:
        assert answers == [None, None, None]
        return
    found = only_last(answers)
    assert found.reason is StuckReason.REPEATED_FAILURE
    assert found.evidence == [
        "pytest tests/test_inv.py failed 3 times with the same KeyError at inv.py:42"
    ]


def test_turns_without_a_failed_command_do_not_break_the_streak() -> None:
    # The agent edits between two runs of the same test, and the edit turn has no failure.
    turns = [
        sig(0, failed_cmd_hash=HASH_A),
        sig(1),
        sig(2, failed_cmd_hash=HASH_A),
        sig(3),
        sig(4, failed_cmd_hash=HASH_A),
    ]
    assert only_last(feed(turns, FAILED)).reason is StuckReason.REPEATED_FAILURE


def test_a_failure_that_changed_breaks_the_streak_and_a_new_one_starts() -> None:
    detector = StuckDetector(LIMITS)
    hashes = [HASH_A, HASH_A, HASH_B, HASH_B]
    assert [detector.observe(sig(i, failed_cmd_hash=h)) for i, h in enumerate(hashes)] == [None] * 4
    # The third failure in a row with the second hash.
    assert detector.observe(sig(4, failed_cmd_hash=HASH_B)) is not None


def test_the_evidence_still_reads_well_without_notes() -> None:
    turns = [sig(i, failed_cmd_hash=HASH_A) for i in range(3)]
    found = only_last(feed(turns))
    assert found.evidence == ["a command failed 3 times with the same output"]


# ---------- EDIT_CHURN ----------
def edit(turn: int, start: int, end: int, path: str = FILE, **fields: Any) -> TurnSignal:
    return sig(turn, edited_regions=[(path, start, end)], **fields)


@pytest.mark.parametrize(
    ("turns", "fires"),
    [
        # Four edits of the same ten lines in four turns, and the tests did not move.
        pytest.param(
            [edit(0, 40, 49, tests_passed=3), edit(1, 40, 52), edit(2, 42, 50), edit(3, 40, 49)],
            True,
            id="pos",
        ),
        # Four edits, but in four different places of the file.
        pytest.param(
            [edit(0, 10, 12), edit(1, 40, 42), edit(2, 80, 82), edit(3, 120, 122)],
            False,
            id="neg",
        ),
    ],
)
def test_edit_churn(turns: list[TurnSignal], fires: bool) -> None:
    answers = feed(turns)
    if not fires:
        assert answers == [None] * len(turns)
        return
    found = only_last(answers)
    assert found.reason is StuckReason.EDIT_CHURN
    assert found.evidence == [
        "src/inv.py lines 40-52 edited 4 times in the last 10 turns with no test change"
    ]


def test_a_region_of_one_line_is_named_by_its_line() -> None:
    turns = [edit(i, 7, 7) for i in range(4)]
    assert only_last(feed(turns)).evidence == [
        "src/inv.py line 7 edited 4 times in the last 10 turns with no test change"
    ]


def test_a_change_in_the_test_count_clears_the_churn() -> None:
    turns = [
        edit(0, 40, 49, tests_passed=3),
        edit(1, 40, 49),
        edit(2, 40, 49, tests_passed=4),
        edit(3, 40, 49),
    ]
    assert feed(turns) == [None] * 4


def test_edits_of_the_same_lines_in_another_file_are_another_region() -> None:
    turns = [edit(i, 40, 49, path=f"src/m{i}.py") for i in range(4)]
    assert feed(turns) == [None] * 4


def test_half_an_overlap_is_the_same_region_and_less_is_not() -> None:
    # Against a ten-line edit, five shared lines are 50% and four are not.
    same = [edit(0, 1, 10), edit(1, 6, 15), edit(2, 1, 10), edit(3, 6, 15)]
    assert only_last(feed(same)).reason is StuckReason.EDIT_CHURN
    apart = [edit(0, 1, 10), edit(1, 7, 16), edit(2, 1, 10), edit(3, 7, 16)]
    # Each of the two places is edited twice, which is below the four the rule needs.
    assert feed(apart) == [None] * 4


def test_a_small_edit_inside_an_earlier_big_one_is_the_same_region() -> None:
    turns = [edit(0, 10, 40), edit(1, 20, 20), edit(2, 25, 26), edit(3, 30, 30)]
    assert only_last(feed(turns)).reason is StuckReason.EDIT_CHURN


def test_edits_older_than_ten_turns_do_not_count() -> None:
    turns = [edit(0, 40, 49), edit(1, 40, 49)]
    turns += [sig(i) for i in range(2, 12)]  # ten quiet turns
    turns += [edit(12, 40, 49), edit(13, 40, 49)]
    assert feed(turns) == [None] * len(turns)


def test_one_turn_can_hold_several_edits_of_the_same_lines() -> None:
    regions = [(FILE, 40, 49)] * 3
    turns = [sig(0, edited_regions=regions), edit(1, 40, 49)]
    assert only_last(feed(turns)).reason is StuckReason.EDIT_CHURN


# ---------- TOOL_ERRORS ----------
@pytest.mark.parametrize(
    ("errors", "fires"),
    [
        pytest.param([1, 1, 1, 1, 1], True, id="pos"),
        # A call that worked in the middle ends the run of errors.
        pytest.param([1, 1, 0, 1, 1], False, id="neg"),
    ],
)
def test_tool_errors(errors: list[int], fires: bool) -> None:
    turns = [sig(i, tool_errors=n) for i, n in enumerate(errors)]
    notes = TurnNotes(tool_error="edit_file: found 0 matches for old_str in src/inv.py")
    answers = feed(turns, notes)
    if not fires:
        assert answers == [None] * 5
        return
    found = only_last(answers)
    assert found.reason is StuckReason.TOOL_ERRORS
    assert found.evidence == [
        "the last 5 tool calls failed in a row",
        "last error: edit_file: found 0 matches for old_str in src/inv.py",
    ]


def test_several_failed_calls_in_one_turn_count_one_by_one() -> None:
    turns = [sig(0, tool_errors=3, tool_calls=3), sig(1, tool_errors=2, tool_calls=2)]
    assert only_last(feed(turns)).reason is StuckReason.TOOL_ERRORS


def test_a_turn_with_one_good_call_is_not_a_turn_of_errors() -> None:
    turns = [sig(i, tool_errors=2, tool_calls=3) for i in range(5)]
    assert feed(turns) == [None] * 5


# ---------- how the rules work together ----------
def test_the_detector_does_not_fire_while_planning() -> None:
    detector = StuckDetector(LIMITS)
    turns = [sig(i, failed_cmd_hash=HASH_A, tool_errors=1) for i in range(20)]
    answers = [detector.observe(t, phase=Phase.PLANNING) for t in turns]
    assert answers == [None] * 20
    # What the planner did is not kept either, so the coder starts with an empty window.
    assert detector.window == []


def test_a_signal_that_fires_empties_the_window() -> None:
    # The same pattern is not reported again on the next turn: the pause of LH-506 starts clean.
    detector = StuckDetector(LIMITS)
    turns = [sig(i, failed_cmd_hash=HASH_A) for i in range(6)]
    answers = [detector.observe(t) for t in turns]
    assert [a is not None for a in answers] == [False, False, True, False, False, True]
    assert len(detector.window) == 0


def test_the_more_specific_rule_wins_when_two_fire_on_one_turn() -> None:
    # Fifteen turns without progress, the last three of them failing the same way: the failure
    # is what a person can act on, so it is the one reported.
    turns = [sig(i, tests_passed=1) for i in range(13)]
    turns += [sig(i, tests_passed=1, failed_cmd_hash=HASH_A) for i in range(13, 16)]
    assert only_last(feed(turns)).reason is StuckReason.REPEATED_FAILURE


def test_the_window_keeps_the_last_30_turns() -> None:
    detector = StuckDetector(stuck.Limits(1000, 1000, 1000, 1000))
    for i in range(50):
        detector.observe(sig(i))
    assert [s.turn_index for s in detector.window] == list(range(20, 50))


def test_a_window_that_is_handed_back_carries_on() -> None:
    first = StuckDetector(LIMITS)
    for i in range(2):
        first.observe(sig(i, failed_cmd_hash=HASH_A), FAILED)
    second = StuckDetector(LIMITS, first.window)
    assert second.observe(sig(2, failed_cmd_hash=HASH_A), FAILED) is not None


def test_the_configured_limits_are_the_ones_the_detector_uses() -> None:
    # The workflow cannot read the config file and keeps a copy (stuck.DEFAULT_LIMITS). A
    # worker whose config differs stops at the start (durable/worker.py).
    config = load_config(ROOT / "config" / "longhaul.example.yaml")
    assert stuck.limits_of(config.stuck) == stuck.DEFAULT_LIMITS
    assert StuckDetector(config.stuck).observe(sig(0)) is None


def test_lower_limits_fire_sooner() -> None:
    detector = StuckDetector(stuck.Limits(2, 2, 2, 2))
    assert [detector.observe(sig(i, failed_cmd_hash=HASH_A)) is not None for i in range(2)] == [
        False,
        True,
    ]


# ---------- the state ----------
# The window as CarryState holds it, dumped the way Temporal dumps the state.
WINDOW_JSON = TypeAdapter(list[TurnSignal])


def worst_turn(turn: int) -> TurnSignal:
    """A turn with every field at its largest, as read_turn would build it."""
    facts = [
        CallSignal(
            tests_passed=99_999,
            failed_cmd_hash=f"{turn:040x}",
            failed_command="x" * 500,
            failed_error="y" * 500,
            edited_regions=[(f"{'d' * 200}/{k}.py", 99_999, 99_999) for k in range(9)],
        )
    ]
    results = [ToolResult(call_id=f"c{k}", ok=False, output="z" * 500) for k in range(40)]
    names = {f"c{k}": "edit_file" for k in range(40)}
    signal, _ = stuck.read_turn(turn + 90_000, 99, facts, results, names)
    return signal


def test_state_size_bound() -> None:
    # The window sits in CarryState, which crosses Continue-As-New, so it has to stay small
    # however long the task runs and however large each field is.
    detector = StuckDetector(stuck.Limits(1000, 1000, 1000, 1000))
    for turn in range(200):
        detector.observe(worst_turn(turn))
    window = detector.window
    assert len(window) == stuck.WINDOW_TURNS == 30
    size = len(WINDOW_JSON.dump_json(window))
    assert size < 8_000, f"the full window is {size} bytes"


def test_a_typical_full_window_is_far_below_the_bound() -> None:
    detector = StuckDetector(stuck.Limits(1000, 1000, 1000, 1000))
    for turn in range(40):
        detector.observe(
            sig(
                turn,
                tests_passed=turn,
                failed_cmd_hash=f"{turn:040x}",
                edited_regions=[("src/calc/inventory.py", 40, 52)],
            )
        )
    assert len(WINDOW_JSON.dump_json(detector.window)) < 6_000


def test_old_edit_regions_are_dropped_because_no_rule_reads_them() -> None:
    detector = StuckDetector(stuck.Limits(1000, 1000, 1000, 1000))
    for turn in range(30):
        detector.observe(edit(turn, 1, 5))
    kept = [bool(s.edited_regions) for s in detector.window]
    assert kept == [False] * 20 + [True] * 10


# ---------- reading a turn ----------
def test_a_turn_is_read_from_the_facts_of_its_calls_and_its_results() -> None:
    facts = [
        CallSignal(edited_regions=[(FILE, 40, 49)]),
        CallSignal(
            tests_passed=3, failed_cmd_hash=HASH_A, failed_command="pytest -q", failed_error="E1"
        ),
        CallSignal(
            tests_passed=4, failed_cmd_hash=HASH_B, failed_command="pytest -q", failed_error="E2"
        ),
    ]
    results = [
        ToolResult(call_id="c1", ok=True, output="diff"),
        ToolResult(call_id="c2", ok=True, output="exit code: 1"),
        ToolResult(call_id="c3", ok=False, output="found 0 matches\nsecond line"),
    ]
    names = {"c1": "edit_file", "c2": "run_tests", "c3": "edit_file"}
    signal, notes = stuck.read_turn(7, 2, facts, results, names)

    assert signal == TurnSignal(
        turn_index=7,
        tests_passed=4,  # the last run of the turn
        steps_done=2,
        failed_cmd_hash=HASH_B,  # the last failed command
        edited_regions=[(FILE, 40, 49)],
        tool_errors=1,
        tool_calls=3,
    )
    assert notes == TurnNotes(
        failed_command="pytest -q", failed_error="E2", tool_error="edit_file: found 0 matches"
    )


def test_a_turn_without_facts_says_nothing_about_tests_or_failures() -> None:
    results = [ToolResult(call_id="c1", ok=True, output="ok")]
    signal, notes = stuck.read_turn(0, 0, [], results, {"c1": "read_file"})
    assert signal == TurnSignal(turn_index=0, tool_calls=1)
    assert notes == TurnNotes()


def test_read_turn_keeps_a_few_short_regions() -> None:
    long_path = "a/" * 60 + "m.py"
    regions = [(long_path, 1, 2)] + [(f"f{k}.py", 1, 2) for k in range(1, 6)]
    signal, _ = stuck.read_turn(0, 0, [CallSignal(edited_regions=regions)], [], {})
    assert len(signal.edited_regions) == stuck.MAX_REGIONS_PER_TURN
    path = signal.edited_regions[0][0]
    assert len(path) <= stuck.MAX_PATH_CHARS
    assert path.endswith("m.py")  # the end of a path says more than its start


def test_notes_are_cut_to_one_short_line() -> None:
    facts = [
        CallSignal(failed_cmd_hash=HASH_A, failed_command="p " * 200, failed_error="e\n" * 200)
    ]
    results = [ToolResult(call_id="c1", ok=False, output="line one\n" + "x" * 500)]
    _, notes = stuck.read_turn(0, 0, facts, results, {"c1": "run_tests"})
    for text in (notes.failed_command, notes.failed_error, notes.tool_error):
        assert text is not None
        assert len(text) <= stuck.MAX_TEXT_CHARS
        assert "\n" not in text


# ---------- the event ----------
def test_the_workflow_records_the_event_that_the_metrics_count() -> None:
    from longhaul.durable.workflows import task
    from longhaul.telemetry import metrics

    assert task.STUCK_EVENT == metrics.STUCK_EVENT == "stuck"
    # The four rules are the reasons the counter has a label for. AGENT_ASKED is LH-506's.
    rules = {r.value for r in StuckReason} - {StuckReason.AGENT_ASKED.value}
    assert rules == set(metrics.STUCK_REASONS)
