"""The result files and the summary of an eval run (LH-901).

Plan section 8.5 gives the formulas. Each number in the README comes from a result file, so the
tests pin how a file is shaped, what the cache ratio is, and what the summary counts.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eval import results
from eval.results import TaskResult, Tokens

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

START = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def make_result(task_id: str = "calc-01", **changes: Any) -> TaskResult:
    values: dict[str, Any] = {
        "run_id": "r1",
        "task_id": task_id,
        "workflow_id": f"lh-eval-r1-{task_id}",
        "task_set": "custom",
        "config": "full",
        "model": "claude-sonnet-5-5",
        "git_sha": "f" * 40,
        "outcome": "pr_opened",
        "outcome_summary": "Changed add().",
        "resolved": True,
        "grading": "all tests passed",
        "cost_usd": 0.5,
        "tokens": Tokens(input=800, output=100, cache_read=200, cache_write=50),
        "cache_ratio": 0.2,
        "started_at": START,
        "ended_at": START + timedelta(seconds=60),
        "wall_time_s": 60.0,
        "turns": 5,
    }
    return TaskResult(**{**values, **changes})


# ---------- cache ratio ----------
def test_cache_ratio_is_cache_read_over_input_plus_cache_read() -> None:
    # Plan 8.5: cache_read / (input + cache_read). Written cache tokens are not part of it.
    assert results.cache_ratio(Tokens(input=800, cache_read=200, cache_write=999)) == 0.2


def test_cache_ratio_of_a_task_without_input_is_zero() -> None:
    assert results.cache_ratio(Tokens()) == 0.0


# ---------- a result file ----------
def test_fields_that_later_phases_fill_start_with_honest_defaults() -> None:
    result = make_result()
    assert (result.compactions, result.verify_rounds) == (0, 0)
    assert result.stuck_reasons == []
    assert result.intervention_needed is False
    assert result.stopped_by_runner is None


def test_a_task_that_was_not_graded_has_no_resolved_value() -> None:
    assert make_result(resolved=None).resolved is None


def test_a_result_file_round_trips(tmp_path: Path) -> None:
    result = make_result()
    path = results.write_result(tmp_path / "r1", result)
    assert path == tmp_path / "r1" / "calc-01.json"
    assert TaskResult.model_validate_json(path.read_text(encoding="utf-8")) == result
    assert results.read_results(tmp_path / "r1") == [result]


def test_the_file_names_every_field_the_plan_asks_for() -> None:
    names = set(make_result().model_dump())
    assert {
        "outcome",
        "resolved",
        "cost_usd",
        "tokens",
        "cache_ratio",
        "wall_time_s",
        "turns",
        "compactions",
        "verify_rounds",
        "stuck_reasons",
        "intervention_needed",
    } <= names


def test_a_result_is_never_overwritten(tmp_path: Path) -> None:
    results.write_result(tmp_path, make_result())
    with pytest.raises(FileExistsError):
        results.write_result(tmp_path, make_result())


def test_results_are_read_in_task_order_and_the_summary_file_is_left_out(tmp_path: Path) -> None:
    for task_id in ("calc-02", "calc-01"):
        results.write_result(tmp_path, make_result(task_id))
    (tmp_path / "summary.json").write_text("{}", encoding="utf-8")
    assert [r.task_id for r in results.read_results(tmp_path)] == ["calc-01", "calc-02"]


def test_a_task_id_cannot_leave_the_run_folder(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="task id"):
        results.write_result(tmp_path, make_result("../escape"))


# ---------- the summary ----------
def summary_of(*items: TaskResult) -> results.Summary:
    return results.summarize(list(items), started_at=START, ended_at=START + timedelta(minutes=5))


def test_the_summary_counts_every_started_task_as_attempted() -> None:
    summary = summary_of(
        make_result("a", resolved=True),
        make_result("b", resolved=False, outcome="error"),
        make_result("c", resolved=False, outcome="cancelled"),
        make_result("d", resolved=None),
    )
    assert summary.attempted == 4
    assert summary.resolved == 1
    assert summary.ungraded == 1
    assert summary.resolve_rate == 0.25


def test_cost_per_resolved_spreads_the_cost_of_every_task_over_the_resolved_ones() -> None:
    summary = summary_of(
        make_result("a", resolved=True, cost_usd=1.0),
        make_result("b", resolved=False, cost_usd=3.0),
    )
    assert summary.cost_usd == 4.0
    assert summary.cost_per_resolved == 4.0


def test_with_nothing_resolved_there_is_no_cost_per_resolved() -> None:
    summary = summary_of(make_result("a", resolved=False, cost_usd=1.0))
    assert summary.cost_per_resolved is None
    assert summary.median_wall_time_s_resolved is None


def test_median_wall_time_is_over_the_resolved_tasks_only() -> None:
    summary = summary_of(
        make_result("a", resolved=True, wall_time_s=10.0),
        make_result("b", resolved=True, wall_time_s=30.0),
        make_result("c", resolved=False, wall_time_s=900.0),
    )
    assert summary.median_wall_time_s_resolved == 20.0


def test_the_cache_ratio_and_the_tokens_are_medians_over_all_tasks() -> None:
    summary = summary_of(
        make_result("a", cache_ratio=0.1, tokens=Tokens(input=100, output=10, cache_read=10)),
        make_result("b", cache_ratio=0.5, tokens=Tokens(input=300, output=30, cache_read=300)),
        make_result("c", cache_ratio=0.9, tokens=Tokens(input=500, output=50, cache_read=900)),
    )
    assert summary.median_cache_ratio == 0.5
    # Plan 8.5: input + output + cache_read, per task.
    assert summary.median_tokens_per_task == 630


def test_intervention_needed_is_a_share_of_the_attempted_tasks() -> None:
    summary = summary_of(
        make_result("a", intervention_needed=True),
        make_result("b"),
        make_result("c"),
        make_result("d"),
    )
    assert summary.intervention_needed == 1
    assert summary.intervention_needed_rate == 0.25


def test_the_summary_names_the_run_and_its_tasks() -> None:
    summary = summary_of(make_result("a"), make_result("b"))
    assert (summary.run_id, summary.task_set, summary.config) == ("r1", "custom", "full")
    assert summary.model == "claude-sonnet-5-5"
    assert summary.tasks == ["a", "b"]
    assert summary.wall_time_s == 300.0


def test_an_empty_run_has_no_summary() -> None:
    with pytest.raises(ValueError, match="no results"):
        results.summarize([], started_at=START, ended_at=START)


def test_results_of_two_runs_are_not_mixed_in_one_summary() -> None:
    with pytest.raises(ValueError, match="run"):
        summary_of(make_result("a"), make_result("b", run_id="r2"))


def test_the_summary_is_written_next_to_the_results(tmp_path: Path) -> None:
    summary = summary_of(make_result("a"))
    path = results.write_summary(tmp_path, summary)
    assert path == tmp_path / "summary.json"
    assert results.Summary.model_validate_json(path.read_text(encoding="utf-8")) == summary
