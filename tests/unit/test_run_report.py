"""The run report: what it says about a task, and that it says it the same way every time (LH-705).

`render_run_report` turns the rows of a task (reports/data.py) into the markdown of
templates/run_report.md.j2. These tests give it rows made in memory, so they need no database.
tests/integration/test_run_report.py reads the same report from Postgres.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from longhaul.reports.data import (
    EventInfo,
    PlanVersion,
    ReportData,
    Spend,
    TaskInfo,
    ToolTotals,
    TurnInfo,
)
from longhaul.reports.render import (
    LESSONS_EVENT,
    MAX_TIMELINE_ROWS,
    render_run_report,
)

from longhaul.contracts import Plan, PlanStep, Usage

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
T0 = datetime(2026, 11, 3, 9, 0, 0, tzinfo=UTC)
ZWSP = "\N{ZERO WIDTH SPACE}"
SECTIONS = ["Overview", "Timeline", "Tool usage", "Plan", "Interventions", "Candidate lessons"]


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def event(seconds: int, type: str, **payload: Any) -> EventInfo:
    return EventInfo(ts=at(seconds), type=type, payload=payload)


def turn(role: str, index: int, start: int, end: int) -> TurnInfo:
    return TurnInfo(role=role, turn_index=index, started_at=at(start), ended_at=at(end))


def a_plan() -> Plan:
    return Plan(
        summary="Fix add()",
        steps=[
            PlanStep(id="s1", description="Change the operator", done_when="test passes"),
            PlanStep(id="s2", description="Add a regression test", done_when="test exists"),
            PlanStep(id="s3", description="Update the readme", done_when="readme mentions add"),
        ],
        acceptance_checks=["pytest -q"],
    )


def a_task(**fields: Any) -> TaskInfo:
    values: dict[str, Any] = {
        "repo": "acme/calc",
        "issue_number": 42,
        "title": "add() subtracts",
        "outcome": "pr_opened",
        "pr_url": None,
        "started_at": at(0),
        "ended_at": at(600),
    }
    return TaskInfo(**{**values, **fields})


def a_run(**fields: Any) -> ReportData:
    """A GitHub task with a plan that a person approved, one failed check and one pause."""
    values: dict[str, Any] = {
        "task_id": "lh-acme-calc-42",
        "task": a_task(),
        "turns": [
            turn("planner", 0, 5, 25),
            turn("planner", 1, 26, 40),
            turn("coder", 0, 200, 230),
            turn("coder", 1, 231, 260),
            turn("coder", 2, 261, 300),
            turn("verifier", 3, 400, 430),
            turn("coder", 3, 440, 470),
            turn("verifier", 4, 500, 530),
        ],
        "tools": [
            ToolTotals(name="run_tests", calls=4, errors=1, total_ms=48_000),
            ToolTotals(name="edit_file", calls=6, errors=0, total_ms=1_200),
            ToolTotals(name="read_file", calls=9, errors=0, total_ms=450),
        ],
        "events": [
            event(
                210,
                "paused",
                reason="repeated_failure",
                pause=1,
                question="Which calc?",
                turn_index=2,
            ),
            event(270, "resumed", pause=1, via="hint", by="alice", turn_index=2),
            event(
                280,
                "compaction",
                turn_index=2,
                forced=False,
                status="ok",
                tokens_before=90_000,
                tokens_after=12_000,
                folded_turns=3,
            ),
            event(
                330,
                "sandbox_recreated",
                head_sha="a" * 40,
                previous_sandbox_id="old",
                sandbox_id="new",
            ),
            event(
                435,
                "verdict",
                round=1,
                passed=False,
                issues=["the regression test is missing"],
                send_back_step="s2",
            ),
            event(535, "verdict", round=2, passed=True, issues=[]),
            event(600, "outcome", kind="pr_opened", repo="acme/calc", turns=8, cost_usd=0.1234),
        ],
        "plans": [
            PlanVersion(
                version=1,
                plan=a_plan(),
                status="approved",
                created_at=at(41),
                decided_by="alice",
                decided_at=at(190),
            )
        ],
        "spend": Spend(
            calls=9,
            usd=Decimal("0.123400"),
            usage=Usage(
                input_tokens=12_000,
                output_tokens=3_400,
                cache_read_tokens=36_000,
                cache_write_tokens=2_000,
            ),
            repo="acme/calc",
        ),
    }
    return ReportData(**{**values, **fields})


def render(data: ReportData | None = None, **options: Any) -> str:
    return render_run_report(data or a_run(), templates=TEMPLATES, **options)


def section(text: str, name: str) -> str:
    """The lines under `### name`, up to the next heading."""
    lines = text.splitlines()
    start = lines.index(f"### {name}") + 1
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("### "):
            break
        body.append(line)
    return "\n".join(body)


def test_the_template_is_in_the_repo() -> None:
    assert (TEMPLATES / "run_report.md.j2").is_file()


def test_every_section_is_there_in_order() -> None:
    text = render()

    headings = [line[4:] for line in text.splitlines() if line.startswith("### ")]
    assert headings == SECTIONS


def test_a_report_with_nothing_to_show_still_has_every_section() -> None:
    text = render(ReportData(task_id="lh-local-x"))

    headings = [line[4:] for line in text.splitlines() if line.startswith("### ")]
    assert headings == SECTIONS
    assert "No plan was made" in section(text, "Plan")
    assert "Nobody stepped in" in section(text, "Interventions")
    assert "No lessons" in section(text, "Candidate lessons")


def test_the_overview_has_the_numbers_of_the_rows() -> None:
    overview = section(render(), "Overview")

    assert "acme/calc" in overview
    assert "issue 42" in overview
    assert "add() subtracts" in overview
    assert "| Outcome | pr opened |" in overview
    assert "| Wall time | 10m 0s |" in overview
    # Counted by role and turn index: planner 0 and 1, coder 0 to 3, verifier 3 and 4.
    assert "| Turns | 8 (planner 2, coder 4, verifier 2) |" in overview
    assert "| Cost | $0.1234 |" in overview
    assert "12,000 input" in overview
    assert "3,400 output" in overview
    assert "36,000 cache read" in overview
    assert "2,000 cache write" in overview
    # 36,000 / (12,000 + 36,000)
    assert "| Cache ratio | 75.0% |" in overview


def test_the_timeline_runs_through_the_phases_in_the_order_they_happened() -> None:
    timeline = section(render(), "Timeline")
    order = [
        "Planning",
        "Approval wait",
        "Coding",
        "Paused",
        "Compaction",
        "Sandbox recreated",
        "Verifying",
        "Verify round 1",
        "Verify round 2",
    ]

    positions = [timeline.index(name) for name in order]
    assert positions == sorted(positions)


def test_the_timeline_says_how_long_each_phase_took_and_who_decided() -> None:
    timeline = section(render(), "Timeline")

    assert "Planning | 2 turns, 35s" in timeline
    assert "Approval wait | plan version 1 approved by alice after 2m 29s" in timeline
    assert "Paused | repeated failure, answered with a hint from alice after 1m 0s" in timeline
    assert "Compaction | 90,000 to 12,000 tokens, 3 turns folded" in timeline
    assert "Sandbox recreated" in timeline
    assert "Verify round 1 | failed, 1 issue: the regression test is missing" in timeline
    assert "Verify round 2 | passed" in timeline


def test_a_pause_nobody_answered_is_shown_as_open() -> None:
    data = a_run(
        events=[event(210, "paused", reason="agent_asked", pause=1, question="Which calc?")]
    )

    assert "agent asked, not answered" in section(render(data), "Timeline")


def test_a_long_timeline_is_cut_with_a_note() -> None:
    folds = [
        event(s, "compaction", tokens_before=9, tokens_after=3, folded_turns=2)
        for s in range(1, MAX_TIMELINE_ROWS + 41)
    ]

    timeline = section(render(a_run(events=folds)), "Timeline")

    rows = [line for line in timeline.splitlines() if line.startswith("| ") and "---" not in line]
    # The header row of the table is one of them.
    assert len(rows) - 1 == MAX_TIMELINE_ROWS
    assert "left out" in timeline


def test_the_tool_table_has_calls_errors_and_time_for_each_tool() -> None:
    tools = section(render(), "Tool usage")

    assert "| run_tests | 4 | 1 | 48.0s |" in tools
    assert "| edit_file | 6 | 0 | 1.2s |" in tools
    assert "| read_file | 9 | 0 | 0.5s |" in tools
    assert "| Total | 19 | 1 | 49.7s |" in tools
    # Busiest first.
    assert tools.index("read_file") < tools.index("edit_file") < tools.index("run_tests")


def test_a_task_that_used_no_tool_says_so() -> None:
    assert "No tool was called." in section(render(a_run(tools=[])), "Tool usage")


def test_the_plan_shows_the_status_each_step_ended_in() -> None:
    steps = [
        event(210, "step_updated", step_id="s1", status="doing", note="", turn_index=0),
        event(240, "step_updated", step_id="s1", status="done", note="", turn_index=1),
        event(310, "step_updated", step_id="s2", status="skipped", note="", turn_index=2),
    ]
    data = a_run()
    data = replace(data, events=[*data.events, *steps])

    plan = section(render(data), "Plan")

    assert "Version 1, approved by alice." in plan
    assert "- [x] **s1** (done): Change the operator" in plan
    assert "- [ ] **s2** (skipped): Add a regression test" in plan
    assert "- [ ] **s3** (todo): Update the readme" in plan


def test_the_plan_in_force_is_the_newest_one_that_was_not_replaced() -> None:
    first = PlanVersion(
        version=1,
        plan=a_plan(),
        status="rejected",
        created_at=at(41),
        decided_by="alice",
        decided_at=at(60),
    )
    second = PlanVersion(
        version=2,
        plan=Plan(
            summary="Fix add() and test it",
            steps=[PlanStep(id="s1", description="Change it and test it", done_when="ok")],
            acceptance_checks=[],
        ),
        status="approved",
        created_at=at(70),
        decided_by="alice",
        decided_at=at(90),
    )

    plan = section(render(a_run(plans=[first, second])), "Plan")

    assert "Version 2, approved by alice." in plan
    assert "Change it and test it" in plan
    assert "Change the operator" not in plan


def test_the_interventions_say_who_did_what_and_when() -> None:
    data = a_run(
        events=[
            event(150, "hint", by="alice", text="Use the copy in src/calc."),
            event(170, "budget_added", by="bob", usd=1.5, turns=10),
            event(580, "cancelled", by="carol", reason="not needed"),
        ]
    )

    text = section(render(data), "Interventions")

    assert "| 2026-11-03 09:03:10 | alice | Approved plan version 1 |" in text
    assert "| 2026-11-03 09:02:30 | alice | Gave a hint: Use the copy in src/calc. |" in text
    assert "| 2026-11-03 09:02:50 | bob | Added $1.50 and 10 turns to the budget |" in text
    assert "| 2026-11-03 09:09:40 | carol | Cancelled the task: not needed |" in text
    assert text.index("09:02:30") < text.index("09:02:50") < text.index("09:03:10")


def test_an_answer_to_a_pause_counts_when_no_hint_event_was_kept() -> None:
    text = section(render(), "Interventions")

    assert "| 2026-11-03 09:04:30 | alice | Answered pause 1 with a hint |" in text


def test_candidate_lessons_come_from_the_lessons_event() -> None:
    lessons = event(
        610,
        LESSONS_EVENT,
        items=[
            {"kind": "pitfall", "content": "calc is installed twice; use src/calc."},
            {"kind": "convention", "content": "Tests live in tests/test_calc.py."},
        ],
    )

    text = section(render(a_run(events=[*a_run().events, lessons])), "Candidate lessons")

    assert "- **pitfall**: calc is installed twice; use src/calc." in text
    assert "- **convention**: Tests live in tests/test_calc.py." in text


def test_the_newest_lessons_event_wins_and_plain_strings_are_accepted() -> None:
    old = event(610, LESSONS_EVENT, items=["an old lesson"])
    new = event(700, LESSONS_EVENT, items=["a new lesson"])

    text = section(render(a_run(events=[old, new])), "Candidate lessons")

    assert "- a new lesson" in text
    assert "old lesson" not in text


def test_a_task_without_a_row_is_reported_from_its_events_and_turns() -> None:
    data = a_run(task=None)

    overview = section(render(data), "Overview")

    assert "local task lh-acme-calc-42" in overview
    assert "| Outcome | pr opened |" in overview
    # From the first row of the task to its outcome event.
    assert "| Wall time | 9m 55s |" in overview
    assert "| Turns | 8 (" in overview


def test_the_outcome_that_the_caller_passes_is_used_when_the_rows_have_none() -> None:
    data = a_run(task=a_task(outcome=None), events=[])

    assert "| Outcome | pr opened |" in section(render(data, outcome="pr_opened"), "Overview")
    assert "| Outcome | running |" in section(render(data), "Overview")


def test_the_wall_time_of_a_task_that_has_not_ended_runs_to_its_last_row() -> None:
    data = a_run(task=a_task(outcome=None, ended_at=None), events=[event(300, "compaction")])

    assert "| Wall time | 8m 50s |" in section(render(data), "Overview")


def test_text_that_people_and_the_model_wrote_cannot_ping_or_close_or_break_a_table() -> None:
    data = a_run(
        task=a_task(title="@bob closes #7 <b>x</b>"),
        events=[
            event(150, "hint", by="mallory", text="line one\n# heading | pipe @carol fixes #9")
        ],
    )

    text = render(data)

    assert "@bob" not in text and f"@{ZWSP}bob" in text
    assert "#7" not in text
    assert "<b>" not in text and "&lt;b>" in text
    assert "@carol" not in text and "#9" not in text
    hint_row = next(line for line in text.splitlines() if "Gave a hint" in line)
    assert "\n" not in hint_row
    assert "\\|" in hint_row, "a pipe in a cell is escaped"


def test_a_secret_in_a_hint_never_reaches_the_report() -> None:
    token = "ghp_" + "a" * 36
    data = a_run(events=[event(150, "hint", by="alice", text=f"use {token} to log in")])

    text = render(data)

    assert token not in text
    assert "[redacted]" in text


def test_the_report_is_the_same_bytes_every_time() -> None:
    assert render() == render()


def test_the_report_has_no_dash_characters_that_the_style_check_bans() -> None:
    text = render()

    assert "—" not in text and "–" not in text


def test_a_long_text_is_cut_to_a_line() -> None:
    data = a_run(events=[event(150, "hint", by="alice", text="word " * 400)])

    row = next(line for line in render(data).splitlines() if "Gave a hint" in line)

    assert len(row) < 400
    assert row.rstrip(" |").endswith("...")
