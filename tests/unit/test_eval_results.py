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
        "config_hash": "a" * 64,
        "seed": 42,
        "model": "claude-sonnet-5-5",
        "git_sha": "f" * 40,
        "provider": None,
        "scripted": False,
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


def test_a_result_says_where_its_answers_came_from() -> None:
    # A scripted run is free and always right. Nothing in the file may pass it off as a real one.
    real = make_result()
    assert (real.provider, real.scripted) == (None, False)
    played = make_result(provider="scripted", scripted=True)
    dumped = played.model_dump(mode="json")
    assert (dumped["provider"], dumped["scripted"]) == ("scripted", True)


def test_a_result_names_the_config_file_it_ran_with_and_the_seed() -> None:
    result = make_result(config_hash="b" * 64, seed=7)
    dumped = result.model_dump(mode="json")
    assert (dumped["config_hash"], dumped["seed"]) == ("b" * 64, 7)
    assert dumped["run_set"] is None


@pytest.mark.parametrize("field", ["config_hash", "seed"])
def test_a_result_cannot_be_written_without_its_config_hash_and_seed(field: str) -> None:
    values = make_result().model_dump()
    del values[field]
    with pytest.raises(ValueError, match=field):
        TaskResult(**values)


@pytest.mark.parametrize("digest", ["", "abc", "g" * 64, "A" * 64, "a" * 65])
def test_a_config_hash_is_a_lowercase_sha256(digest: str) -> None:
    with pytest.raises(ValueError, match="config_hash"):
        make_result(config_hash=digest)


def test_the_sliding_window_is_a_context_mode_a_result_can_record() -> None:
    assert make_result(context_mode="sliding").context_mode == "sliding"


def test_a_result_must_say_whether_it_was_scripted() -> None:
    values = make_result().model_dump()
    del values["scripted"]
    with pytest.raises(ValueError, match="scripted"):
        TaskResult(**values)


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


def test_the_summary_names_the_config_file_the_seed_and_the_run_set() -> None:
    summary = summary_of(
        make_result("a", config_hash="c" * 64, seed=7, run_set="headline"),
        make_result("b", config_hash="c" * 64, seed=7, run_set="headline"),
    )
    assert (summary.config_hash, summary.seed, summary.run_set) == ("c" * 64, 7, "headline")


@pytest.mark.parametrize(
    ("field", "other"), [("config_hash", "d" * 64), ("seed", 8), ("run_set", "other")]
)
def test_results_of_two_config_files_or_seeds_are_not_one_run(field: str, other: object) -> None:
    with pytest.raises(ValueError, match=field):
        summary_of(make_result("a"), make_result("b", **{field: other}))


def test_a_result_says_whether_mixed_models_were_allowed() -> None:
    assert make_result().allow_mixed_models is False
    assert make_result(allow_mixed_models=True).model_dump()["allow_mixed_models"] is True


def test_a_result_file_from_before_the_switch_was_recorded_still_reads(tmp_path: Path) -> None:
    results.write_result(tmp_path, make_result())
    path = tmp_path / "calc-01.json"
    old = path.read_text(encoding="utf-8").replace('  "allow_mixed_models": false,\n', "")
    assert "allow_mixed_models" not in old
    path.write_text(old, encoding="utf-8")

    assert results.read_results(tmp_path)[0].allow_mixed_models is False


def test_the_summary_says_whether_mixed_models_were_allowed() -> None:
    assert summary_of(make_result("a")).allow_mixed_models is False
    allowed = summary_of(
        make_result("a", allow_mixed_models=True), make_result("b", allow_mixed_models=True)
    )
    assert allowed.allow_mixed_models is True


def test_results_that_disagree_on_mixed_models_are_not_one_run() -> None:
    with pytest.raises(ValueError, match="allow_mixed_models"):
        summary_of(make_result("a"), make_result("b", allow_mixed_models=True))


def summary_on(model: str, run_id: str, *, mixed: bool = False) -> results.Summary:
    return summary_of(make_result("a", model=model, run_id=run_id, allow_mixed_models=mixed))


def test_runs_on_one_model_can_be_compared() -> None:
    results.check_comparable(
        [summary_on("claude-sonnet-5-5", "full-1"), summary_on("claude-sonnet-5-5", "naive-1")]
    )


def test_runs_on_two_models_cannot_be_compared_unless_a_run_allowed_it() -> None:
    sonnet = summary_on("claude-sonnet-5-5", "full-1")
    haiku = summary_on("claude-haiku-5-5", "naive-1")

    with pytest.raises(ValueError, match="full-1.*naive-1|naive-1.*full-1") as caught:
        results.check_comparable([sonnet, haiku])
    assert "--allow-mixed-models" in str(caught.value)

    results.check_comparable([sonnet, summary_on("claude-haiku-5-5", "naive-1", mixed=True)])


def test_the_summary_names_the_run_and_its_tasks() -> None:
    summary = summary_of(make_result("a"), make_result("b"))
    assert (summary.run_id, summary.task_set, summary.config) == ("r1", "custom", "full")
    assert summary.model == "claude-sonnet-5-5"
    assert summary.tasks == ["a", "b"]
    assert summary.wall_time_s == 300.0


def test_the_summary_says_where_the_answers_came_from() -> None:
    real = summary_of(make_result("a"))
    assert (real.provider, real.scripted) == (None, False)
    played = summary_of(
        make_result("a", provider="scripted", scripted=True),
        make_result("b", provider="scripted", scripted=True),
    )
    assert (played.provider, played.scripted) == ("scripted", True)


def test_scripted_and_real_results_are_not_mixed_in_one_summary() -> None:
    with pytest.raises(ValueError, match="scripted"):
        summary_of(make_result("a"), make_result("b", provider="scripted", scripted=True))


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


# ---------- a verdict that comes later (LH-902) ----------
def test_a_verdict_is_written_into_a_result_that_exists(tmp_path: Path) -> None:
    results.write_result(tmp_path, make_result(resolved=None, grading="not graded yet"))

    changed = results.set_verdict(tmp_path, "calc-01", resolved=True, grading="resolved")

    assert (changed.resolved, changed.grading) == (True, "resolved")
    assert results.read_results(tmp_path) == [changed]
    assert changed.model_dump(exclude={"resolved", "grading"}) == make_result().model_dump(
        exclude={"resolved", "grading"}
    )


def test_a_verdict_can_be_replaced_by_a_later_one(tmp_path: Path) -> None:
    results.write_result(tmp_path, make_result(resolved=True))

    results.set_verdict(tmp_path, "calc-01", resolved=None, grading="the harness failed")

    assert results.read_results(tmp_path)[0].resolved is None


def test_a_verdict_for_a_task_without_a_result_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="calc-01"):
        results.set_verdict(tmp_path, "calc-01", resolved=True, grading="resolved")


def test_a_verdict_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    results.write_result(tmp_path, make_result())

    results.set_verdict(tmp_path, "calc-01", resolved=False, grading="no")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["calc-01.json"]


# ---------- the run set ----------
def write_run(
    root: Path, run_id: str, *, model: str, run_set: str | None, config: str = "full"
) -> None:
    results.write_result(
        root / run_id,
        make_result(run_id=run_id, model=model, run_set=run_set, config=config),
    )


def test_the_models_of_a_run_set_are_read_from_the_runs_in_the_results_folder(
    tmp_path: Path,
) -> None:
    write_run(tmp_path, "full-1", model="m1", run_set="headline")
    write_run(tmp_path, "naive-1", model="m1", run_set="headline", config="naive")
    write_run(tmp_path, "other-1", model="m2", run_set="ablation")
    write_run(tmp_path, "alone-1", model="m3", run_set=None)

    found = results.models_in_run_set(tmp_path, "headline")

    assert found == {"full-1": "m1", "naive-1": "m1"}


def test_a_folder_that_is_not_a_run_is_skipped_when_the_run_set_is_read(tmp_path: Path) -> None:
    write_run(tmp_path, "full-1", model="m1", run_set="headline")
    (tmp_path / "chaos-2026-11-04").mkdir()
    (tmp_path / "chaos-2026-11-04" / "summary.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("not a run", encoding="utf-8")
    (tmp_path / "empty").mkdir()

    assert results.models_in_run_set(tmp_path, "headline") == {"full-1": "m1"}


def test_a_results_folder_that_does_not_exist_holds_no_runs(tmp_path: Path) -> None:
    assert results.models_in_run_set(tmp_path / "nowhere", "headline") == {}
