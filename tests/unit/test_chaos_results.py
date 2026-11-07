"""The result of a chaos run and the summary of the suite (LH-904).

Every number the project quotes about surviving crashes comes from `summary.json`, so the tests
pin what a run records, how the totals are added up, and that nothing is left out of the file.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eval.chaos import results
from eval.chaos.results import KillResult, RunResult, SideEffects

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

START = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
EXPECTED = SideEffects(comments=1, label_events=3, pull_requests=1)


def kill(kind: str = "worker", latency: float | None = 6.0, landed: bool = True) -> KillResult:
    return KillResult(
        type=kind,
        landed=landed,
        place="tool_exec",
        struck_at=START if landed else None,
        resume_latency_s=latency if landed else None,
    )


def run_result(index: int = 1, **changes: Any) -> RunResult:
    values: dict[str, Any] = {
        "run": index,
        "task_id": "calc-01",
        "workflow_id": f"lh-chaos-calc-01-{index:08x}",
        "seed": 7000 + index,
        "kills_planned": ["worker"],
        "kills": [kill()],
        "completed": True,
        "outcome": "pr_opened",
        "diff_equals_reference": True,
        "problems": [],
        "ledger_rows": 5,
        "extra_ledger_rows": 0,
        "cost_usd": 0.5,
        "extra_cost_usd": 0.0,
        "resume_latency_s": [6.0],
        "side_effects": EXPECTED,
        "expected_side_effects": EXPECTED,
        "duplicate_side_effects": 0,
        "missing_side_effects": 0,
        "wall_time_s": 90.0,
    }
    return RunResult(**{**values, **changes})


def summary_of(found: list[RunResult], **changes: Any) -> results.Summary:
    values: dict[str, Any] = {
        "name": "chaos-2026-11-05",
        "seed": 7,
        "git_sha": "f" * 40,
        "tasks": ["calc-01"],
        "recordings": {"calc-01": "scripted"},
        "started_at": START,
        "ended_at": START + timedelta(minutes=30),
        "clock_offset_s": 0.05,
        "compose_project": "longhaul-chaos-ab12cd34",
    }
    return results.summarize(found, **{**values, **changes})


# ---------- the totals ----------
def test_the_summary_counts_runs_that_completed_with_the_reference_diff() -> None:
    found = [
        run_result(1),
        run_result(2, completed=False, outcome="error", diff_equals_reference=False),
        run_result(3, diff_equals_reference=False),
    ]

    summary = summary_of(found)

    assert summary.runs == 3
    assert summary.completed == 2
    assert summary.completion_rate == pytest.approx(2 / 3)
    assert summary.diff_matches == 1
    assert summary.survived == 1


def test_the_summary_counts_the_kills_by_type_planned_and_landed() -> None:
    found = [
        run_result(1, kills_planned=["worker", "sandbox"], kills=[kill("worker"), kill("sandbox")]),
        run_result(
            2,
            kills_planned=["worker", "postgres"],
            kills=[kill("worker"), kill("postgres", landed=False)],
        ),
    ]

    summary = summary_of(found)

    assert summary.kills_planned == {"worker": 2, "sandbox": 1, "temporal": 0, "postgres": 1}
    assert summary.kills_landed == {"worker": 2, "sandbox": 1, "temporal": 0, "postgres": 0}


def test_extra_ledger_rows_and_their_cost_are_added_up() -> None:
    found = [
        run_result(1, extra_ledger_rows=1, extra_cost_usd=0.25),
        run_result(2),
        run_result(3, extra_ledger_rows=2, extra_cost_usd=0.5),
    ]

    summary = summary_of(found)

    assert summary.extra_ledger_rows == 3
    assert summary.extra_cost_usd == pytest.approx(0.75)


def test_the_summary_adds_up_the_duplicate_and_missing_side_effects() -> None:
    found = [
        run_result(1, duplicate_side_effects=1),
        run_result(2, missing_side_effects=2),
        run_result(3),
    ]

    summary = summary_of(found)

    assert summary.duplicate_side_effects == 1
    assert summary.missing_side_effects == 2
    assert summary.runs_with_side_effect_mismatch == 2


# ---------- the latency ----------
def test_latency_stats_say_how_many_and_how_slow() -> None:
    stats = results.latency_stats([1.0, 2.0, 3.0, 4.0, 100.0])

    assert stats.count == 5
    assert stats.median_s == 3.0
    assert stats.max_s == 100.0
    assert stats.p95_s == 100.0


def test_the_p95_is_the_value_at_the_95th_percent_rank() -> None:
    values = [float(n) for n in range(1, 21)]

    assert results.latency_stats(values).p95_s == 19.0


def test_no_latency_is_stats_of_nothing() -> None:
    stats = results.latency_stats([])

    assert stats == results.LatencyStats(count=0, median_s=None, p95_s=None, max_s=None)


def test_the_summary_has_the_latency_overall_and_for_each_kind_of_kill() -> None:
    found = [
        run_result(
            1,
            kills_planned=["worker", "temporal"],
            kills=[kill("worker", 6.0), kill("temporal", 15.0)],
            resume_latency_s=[6.0, 15.0],
        ),
        run_result(2, kills=[kill("worker", 8.0)], resume_latency_s=[8.0]),
    ]

    summary = summary_of(found)

    assert summary.resume_latency.count == 3
    assert summary.resume_latency.median_s == 8.0
    assert summary.resume_latency_by_type["worker"].count == 2
    assert summary.resume_latency_by_type["temporal"].max_s == 15.0
    assert summary.resume_latency_by_type["sandbox"].count == 0


def test_a_kill_with_no_later_activity_has_no_latency_and_is_left_out_of_the_stats() -> None:
    found = [run_result(1, kills=[kill("worker", None)], resume_latency_s=[])]

    summary = summary_of(found)

    assert summary.resume_latency.count == 0


# ---------- the files ----------
def test_the_summary_says_how_often_the_watchdog_restarted_temporal() -> None:
    # A restart that the plan did not draw is part of what the runs met, so it is reported.
    assert summary_of([run_result()]).temporal_heals == 0
    assert summary_of([run_result()], temporal_heals=2).temporal_heals == 2


def test_a_summary_cannot_be_made_of_no_runs() -> None:
    with pytest.raises(ValueError, match="no runs"):
        summary_of([])


def test_the_summary_file_has_every_field_that_the_story_asks_for(tmp_path: Path) -> None:
    summary = summary_of([run_result()])

    path = results.write_summary(tmp_path, summary)
    written = json.loads(path.read_text(encoding="utf-8"))

    assert path == tmp_path / "summary.json"
    assert set(results.SUMMARY_FIELDS) <= set(written)
    (only,) = written["results"]
    assert set(results.RUN_FIELDS) <= set(only)
    assert set(results.KILL_FIELDS) <= set(only["kills"][0])


def test_every_field_of_a_run_is_in_the_list_of_fields() -> None:
    assert set(RunResult.model_fields) == set(results.RUN_FIELDS)
    assert set(KillResult.model_fields) == set(results.KILL_FIELDS)
    assert set(results.Summary.model_fields) == set(results.SUMMARY_FIELDS)


def test_a_run_file_is_written_once(tmp_path: Path) -> None:
    first = results.write_run(tmp_path, run_result(3))

    assert first.name == "run-03.json"
    assert json.loads(first.read_text(encoding="utf-8"))["run"] == 3
    with pytest.raises(FileExistsError):
        results.write_run(tmp_path, run_result(3))


def test_the_summary_can_be_read_back() -> None:
    summary = summary_of([run_result(1), run_result(2)])

    again = results.Summary.model_validate_json(summary.model_dump_json())

    assert again == summary


def test_a_result_with_a_field_nobody_knows_is_refused() -> None:
    with pytest.raises(ValueError, match="extra"):
        RunResult.model_validate({**run_result().model_dump(), "luck": 1})
