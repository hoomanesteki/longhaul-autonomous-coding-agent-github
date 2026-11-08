"""The run report as markdown (LH-705).

A reviewer opens a pull request and wants to know how the agent got there and what it cost. The
report answers in six sections, from the rows of reports/data.py and the template
templates/run_report.md.j2: the overview (issue, outcome, wall time, turns, cost, tokens, cache
ratio), the timeline of the phases, the tool usage, the plan with the status of each step, the
interventions of people, and the candidate lessons.

The rules this module keeps:

* **The same bytes every time.** `deliver` puts the report in the body of a pull request, and
  `create_pr` refuses a reused key with other text (github/side_effects.py). Nothing here reads
  a clock, and every list is in the order of the rows.
* **A short report.** The timeline stops at MAX_TIMELINE_ROWS and says how many entries it left
  out, the tool table at MAX_TOOL_ROWS, and a text is cut to one line. A task of 500 turns gets a
  report about as long as a task of 10.
* **Only the template can mention a person or close an issue.** The title, the hints, the
  reasons and the lessons are written by people and a model. `_text` ends the effect of "@name",
  "#7" and "<" (github/pr_body.defuse), folds line breaks, and escapes a pipe so that a text
  cannot end a table cell. The whole report goes through `redact` (telemetry/logging.py) at the
  end, so a key that a hint or a title carried never reaches a page that people read (LH-706).
* **No heading above the third level and no code fence.** The report sits inside a `<details>`
  block of the pull request, and a fence that a cut left open would swallow the rest of the body.

The phases are read from the rows and not from a phase field, because the rows are what exists
for every task: the turns of each role say when it planned, coded and verified, the plan row says
when a person decided, and the events say what else happened. Event types are written out here
because the workflow cannot be imported (it brings the Temporal sandbox with it). A unit test
keeps each name equal to the one the writer uses.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from longhaul.context.tokens import cache_ratio
from longhaul.github.pr_body import defuse
from longhaul.reports.data import EventInfo, PlanVersion, ReportData, ToolTotals, TurnInfo
from longhaul.telemetry.logging import redact

__all__ = [
    "LESSONS_EVENT",
    "MAX_TIMELINE_ROWS",
    "TEMPLATE",
    "render_run_report",
]

TEMPLATE = "run_report.md.j2"

# What keeps the report short.
MAX_TIMELINE_ROWS = 60
MAX_TOOL_ROWS = 25
MAX_INTERVENTIONS = 30
MAX_LESSONS = 10
MAX_STEPS = 20
LINE_CHARS = 160

# The event types the report reads (plan section 3.5). `lessons` is recorded by the lesson
# extraction of LH-605: its payload has `items`, a list of {kind, content} or of plain strings.
PAUSED_EVENT = "paused"
RESUMED_EVENT = "resumed"
COMPACTION_EVENT = "compaction"
SANDBOX_EVENT = "sandbox_recreated"
STUCK_EVENT = "stuck"
VERDICT_EVENT = "verdict"
HAND_OVER_EVENT = "continue_as_new"
STEP_EVENT = "step_updated"
OUTCOME_EVENT = "outcome"
HINT_EVENT = "hint"
BUDGET_EVENT = "budget_added"
CANCELLED_EVENT = "cancelled"
LESSONS_EVENT = "lessons"

# The roles in the order a task meets them, for the turn counts of the overview.
ROLE_ORDER = ("planner", "coder", "verifier")
SEGMENT_LABELS = {"planner": "Planning", "coder": "Coding", "verifier": "Verifying"}
# A plan in one of these states is not the plan in force.
GONE = frozenset({"rejected", "superseded"})
ANSWERS = {"hint": "a hint", "budget": "a budget top-up"}
TEN_THOUSANDTH = Decimal("0.0001")


@lru_cache(maxsize=4)
def _environment(templates: Path) -> Environment:
    # Markdown, not HTML, so the engine escapes nothing. The values are made safe below.
    # StrictUndefined turns a field that the template names and the code forgot into an error.
    return Environment(  # noqa: S701
        loader=FileSystemLoader(templates),
        autoescape=False,
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )


# ---------- text ----------
def _text(value: object, limit: int = LINE_CHARS) -> str:
    """Words from a person or a model, on one line, safe in a table cell and on a page."""
    folded = " ".join(str(value).split())
    line = folded[:limit].rstrip() + ("..." if len(folded) > limit else "")
    return defuse(line).replace("|", "\\|")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _duration(seconds: float) -> str:
    whole = max(int(seconds), 0)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def _tool_time(ms: int) -> str:
    if ms >= 60_000:
        return _duration(ms // 1000)
    tenths = (ms + 50) // 100  # half up, in integers: a float would round 49.65 down
    return f"{tenths // 10}.{tenths % 10}s"


def _stamp(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _words(name: object) -> str:
    return str(name).replace("_", " ")


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# ---------- the overview ----------
@dataclass(frozen=True)
class _Span:
    start: datetime | None
    end: datetime | None


def _span(data: ReportData) -> _Span:
    """From the start of the task to its end, or to the last row it wrote if it has not ended."""
    stamps = [t.started_at for t in data.turns] + [e.ts for e in data.events]
    start = (
        data.task.started_at if data.task and data.task.started_at else min(stamps, default=None)
    )
    ended = [e.ts for e in data.events if e.type == OUTCOME_EVENT]
    if ended:
        return _Span(start, ended[-1])
    if data.task and data.task.ended_at:
        return _Span(start, data.task.ended_at)
    last = [t.ended_at or t.started_at for t in data.turns] + [e.ts for e in data.events]
    return _Span(start, max(last, default=None))


def _wall_time(span: _Span) -> str:
    if span.start is None or span.end is None:
        return "unknown"
    return _duration((span.end - span.start).total_seconds())


def _outcome(data: ReportData, given: str | None) -> str:
    kinds = [e.payload.get("kind") for e in data.events if e.type == OUTCOME_EVENT]
    named = [k for k in kinds if isinstance(k, str)]
    found = named[-1] if named else (data.task.outcome if data.task else None)
    return _words(found or given or "running")


def _issue(data: ReportData) -> str:
    if data.task is None:
        return _text(f"local task {data.task_id}")
    where = data.task.repo
    if data.task.issue_number is not None:
        where += f", issue {data.task.issue_number}"
    return _text(f"{data.task.title} ({where})")


def _turn_counts(turns: Iterable[TurnInfo]) -> str:
    pairs = {(t.role, t.turn_index) for t in turns}
    by_role = Counter(role for role, _ in pairs)
    if not pairs:
        return "0"
    roles = sorted(by_role, key=lambda r: (ROLE_ORDER.index(r) if r in ROLE_ORDER else 99, r))
    return f"{len(pairs)} ({', '.join(f'{r} {by_role[r]}' for r in roles)})"


def _overview(data: ReportData, outcome: str | None) -> list[dict[str, str]]:
    usage = data.spend.usage
    cost = data.spend.usd.quantize(TEN_THOUSANDTH, rounding=ROUND_HALF_UP)
    tokens = (
        f"{usage.input_tokens:,} input, {usage.output_tokens:,} output, "
        f"{usage.cache_read_tokens:,} cache read, {usage.cache_write_tokens:,} cache write"
    )
    rows = [
        ("Issue", _issue(data)),
        ("Outcome", _text(_outcome(data, outcome))),
        ("Wall time", _wall_time(_span(data))),
        ("Turns", _turn_counts(data.turns)),
        ("Cost", f"${cost}"),
        ("Tokens", tokens),
        ("Cache ratio", f"{cache_ratio(usage):.1%}"),
    ]
    return [{"label": label, "value": value} for label, value in rows]


# ---------- the timeline ----------
@dataclass(frozen=True)
class _Entry:
    ts: datetime
    label: str
    detail: str


def _segments(turns: list[TurnInfo]) -> list[_Entry]:
    """One entry for each run of turns of one role: planning, coding, or a verifier pass."""
    entries: list[_Entry] = []
    run: list[TurnInfo] = []
    for item in [*sorted(turns, key=lambda t: (t.started_at, t.turn_index)), None]:
        if run and (item is None or item.role != run[0].role):
            entries.append(_segment(run))
            run = []
        if item is not None:
            run.append(item)
    return entries


def _segment(run: list[TurnInfo]) -> _Entry:
    first, count = run[0], len({t.turn_index for t in run})
    end = max(t.ended_at or t.started_at for t in run)
    label = SEGMENT_LABELS.get(first.role, first.role.capitalize())
    detail = f"{_plural(count, 'turn')}, {_duration((end - first.started_at).total_seconds())}"
    return _Entry(first.started_at, label, detail)


def _approval(plans: list[PlanVersion]) -> list[_Entry]:
    return [
        _Entry(
            p.created_at,
            "Approval wait",
            _text(
                f"plan version {p.version} {p.status} by {p.decided_by} after "
                f"{_duration((p.decided_at - p.created_at).total_seconds())}"
            ),
        )
        for p in plans
        if p.decided_at is not None and p.decided_by and p.status in ("approved", "rejected")
    ]


def _pause(event: EventInfo, answers: Mapping[Any, EventInfo]) -> _Entry:
    reason = _words(event.payload.get("reason", "unknown"))
    answer = answers.get(event.payload.get("pause"))
    if answer is None:
        return _Entry(event.ts, "Paused", _text(f"{reason}, not answered"))
    via = ANSWERS.get(str(answer.payload.get("via")), "an answer")
    who = answer.payload.get("by")
    waited = _duration((answer.ts - event.ts).total_seconds())
    by = f" from {who}" if who else ""
    return _Entry(event.ts, "Paused", _text(f"{reason}, answered with {via}{by} after {waited}"))


def _compaction(event: EventInfo) -> _Entry:
    p = event.payload
    before, after, folded = (
        _int(p.get("tokens_before")),
        _int(p.get("tokens_after")),
        p.get("folded_turns"),
    )
    detail = "compacted"
    if before is not None and after is not None:
        detail = f"{before:,} to {after:,} tokens"
    if (n := _int(folded)) is not None:
        detail += f", {_plural(n, 'turn')} folded"
    if p.get("forced") is True:
        detail += ", forced"
    return _Entry(event.ts, "Compaction", detail)


def _verdict(event: EventInfo, counted: int) -> _Entry:
    p = event.payload
    number = _int(p.get("round")) or counted
    issues = [str(i) for i in p.get("issues") or []]
    if p.get("passed") is True:
        detail = "passed"
    elif issues:
        detail = f"failed, {_plural(len(issues), 'issue')}: {_text(issues[0], 80)}"
    else:
        detail = "failed"
    return _Entry(event.ts, f"Verify round {number}", detail)


def _event_entries(events: list[EventInfo]) -> list[_Entry]:
    answers = {e.payload.get("pause"): e for e in events if e.type == RESUMED_EVENT}
    entries: list[_Entry] = []
    rounds = 0
    for e in events:
        if e.type == PAUSED_EVENT:
            entries.append(_pause(e, answers))
        elif e.type == COMPACTION_EVENT:
            entries.append(_compaction(e))
        elif e.type == SANDBOX_EVENT:
            sandbox = e.payload.get("sandbox_id")
            detail = (
                f"replaced by {_text(sandbox, 40)}" if sandbox else "a lost sandbox was rebuilt"
            )
            entries.append(_Entry(e.ts, "Sandbox recreated", detail))
        elif e.type == STUCK_EVENT:
            entries.append(_Entry(e.ts, "Stuck", _text(_words(e.payload.get("reason", "unknown")))))
        elif e.type == HAND_OVER_EVENT:
            reason = _text(_words(e.payload.get("reason", "turns")))
            entries.append(_Entry(e.ts, "Hand-over", f"to a new run ({reason})"))
        elif e.type == VERDICT_EVENT:
            rounds += 1
            entries.append(_verdict(e, rounds))
        elif e.type == OUTCOME_EVENT:
            entries.append(_Entry(e.ts, "Ended", _text(_words(e.payload.get("kind", "unknown")))))
    return entries


def _timeline(data: ReportData, start: datetime | None) -> tuple[list[dict[str, str]], int]:
    found = [*_segments(data.turns), *_approval(data.plans), *_event_entries(data.events)]
    found.sort(key=lambda entry: entry.ts)  # stable: a tie keeps the order above
    origin = start or (found[0].ts if found else None)
    shown = [
        {
            "at": "+" + _duration((entry.ts - origin).total_seconds() if origin else 0),
            "label": entry.label,
            "detail": entry.detail,
        }
        for entry in found[:MAX_TIMELINE_ROWS]
    ]
    return shown, max(len(found) - MAX_TIMELINE_ROWS, 0)


# ---------- the tools ----------
def _tools(tools: list[ToolTotals]) -> dict[str, Any]:
    ordered = sorted(tools, key=lambda t: (-t.calls, t.name))
    rows = [
        {
            "name": _text(t.name, 60),
            "calls": str(t.calls),
            "errors": str(t.errors),
            "time": _tool_time(t.total_ms),
        }
        for t in ordered[:MAX_TOOL_ROWS]
    ]
    total = {
        "calls": str(sum(t.calls for t in tools)),
        "errors": str(sum(t.errors for t in tools)),
        "time": _tool_time(sum(t.total_ms for t in tools)),
    }
    return {"rows": rows, "total": total, "hidden": max(len(tools) - MAX_TOOL_ROWS, 0)}


# ---------- the plan ----------
def _plan(data: ReportData) -> dict[str, Any] | None:
    if not data.plans:
        return None
    live = [p for p in data.plans if p.status not in GONE]
    current = (live or data.plans)[-1]
    status: dict[str, str] = {s.id: s.status for s in current.plan.steps}
    for e in data.events:
        step = e.payload.get("step_id")
        if e.type == STEP_EVENT and step in status:
            status[step] = str(e.payload.get("status", status[step]))
    by = f" by {_text(current.decided_by, 60)}" if current.decided_by else ""
    steps = [
        {
            "box": "x" if status[s.id] == "done" else " ",
            "id": _text(s.id, 30),
            "status": _text(status[s.id], 20),
            "description": _text(s.description),
        }
        for s in current.plan.steps[:MAX_STEPS]
    ]
    return {
        "heading": f"Version {current.version}, {current.status}{by}.",
        "summary": _text(current.plan.summary),
        "steps": steps,
        "hidden": max(len(current.plan.steps) - MAX_STEPS, 0),
    }


# ---------- the interventions ----------
@dataclass(frozen=True)
class _Step:
    ts: datetime
    who: str
    what: str


def _budget_text(payload: Mapping[str, Any]) -> str:
    usd, turns = payload.get("usd"), _int(payload.get("turns"))
    parts = []
    if isinstance(usd, int | float) and usd:
        parts.append(f"${usd:.2f}")
    if turns:
        parts.append(_plural(turns, "turn"))
    return f"Added {' and '.join(parts) or 'nothing'} to the budget"


def _decisions(plans: list[PlanVersion]) -> list[_Step]:
    steps = []
    for p in plans:
        if p.decided_at is None or not p.decided_by:
            continue
        verb = {"approved": "Approved", "rejected": "Rejected"}.get(p.status, "Decided on")
        steps.append(_Step(p.decided_at, p.decided_by, f"{verb} plan version {p.version}"))
    return steps


def _said(events: list[EventInfo]) -> list[_Step]:
    """The hints, budget top-ups, cancels and answers to a pause, in the order they were made."""
    direct = any(e.type in (HINT_EVENT, BUDGET_EVENT) for e in events)
    steps = []
    for e in events:
        who = str(e.payload.get("by") or "unknown")
        if e.type == HINT_EVENT:
            steps.append(_Step(e.ts, who, f"Gave a hint: {_text(e.payload.get('text', ''))}"))
        elif e.type == BUDGET_EVENT:
            steps.append(_Step(e.ts, who, _budget_text(e.payload)))
        elif e.type == CANCELLED_EVENT:
            why = e.payload.get("reason")
            steps.append(
                _Step(e.ts, who, "Cancelled the task" + (f": {_text(why)}" if why else ""))
            )
        elif e.type == RESUMED_EVENT and not direct and e.payload.get("by"):
            via = ANSWERS.get(str(e.payload.get("via")), "an answer")
            steps.append(
                _Step(e.ts, who, f"Answered pause {e.payload.get('pause', '?')} with {via}")
            )
    return steps


def _interventions(data: ReportData) -> tuple[list[dict[str, str]], int]:
    found = sorted([*_decisions(data.plans), *_said(data.events)], key=lambda s: s.ts)
    rows = [
        {"when": _stamp(s.ts), "who": _text(s.who, 60), "what": _text(s.what, LINE_CHARS + 20)}
        for s in found[:MAX_INTERVENTIONS]
    ]
    return rows, max(len(found) - MAX_INTERVENTIONS, 0)


# ---------- the lessons ----------
def _lesson(item: object) -> str | None:
    if isinstance(item, str):
        text, kind = item, None
    elif isinstance(item, Mapping):
        text = next((item[k] for k in ("content", "text", "lesson") if item.get(k)), "")
        kind = item.get("kind")
    else:
        return None
    if not str(text).strip():
        return None
    return f"**{_text(kind, 20)}**: {_text(text)}" if kind else _text(text)


def _lessons(events: list[EventInfo]) -> tuple[list[str], int]:
    """The items of the newest lessons event. Each extraction replaces the one before it."""
    recorded = [e for e in events if e.type == LESSONS_EVENT]
    if not recorded:
        return [], 0
    payload = recorded[-1].payload
    items = payload.get("items", payload.get("lessons", []))
    lines = [line for line in map(_lesson, items if isinstance(items, list) else []) if line]
    return lines[:MAX_LESSONS], max(len(lines) - MAX_LESSONS, 0)


# ---------- the report ----------
def render_run_report(data: ReportData, *, templates: Path, outcome: str | None = None) -> str:
    """The markdown report of a task.

    `outcome` is what to call the end of a task whose rows have no outcome yet. `deliver` passes
    `pr_opened`, because it runs before the workflow records the outcome event. `templates` is
    the folder that holds run_report.md.j2 (settings.templates_path).
    """
    timeline, timeline_hidden = _timeline(data, _span(data).start)
    interventions, interventions_hidden = _interventions(data)
    lessons, lessons_hidden = _lessons(data.events)
    page = (
        _environment(templates)
        .get_template(TEMPLATE)
        .render(
            overview=_overview(data, outcome),
            timeline=timeline,
            timeline_hidden=timeline_hidden,
            tools=_tools(data.tools),
            plan=_plan(data),
            interventions=interventions,
            interventions_hidden=interventions_hidden,
            lessons=lessons,
            lessons_hidden=lessons_hidden,
        )
    )
    return redact(str(page))
