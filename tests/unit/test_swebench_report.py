"""Reading the report of the official harness into the result files (LH-902).

The reports here have the shape that swebench 5.0.2 writes (harness/reporting.py, schema_version
2). The harness itself runs in tests/integration/test_swebench_adapter.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eval import results
from eval.results import TaskResult, Tokens
from eval.swebench import report

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

START = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
IDS = ["a__b-1", "a__b-2", "a__b-3", "a__b-4", "a__b-5", "a__b-6"]


def harness_report(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "total_instances": 500,
        "submitted_instances": 6,
        "completed_instances": 2,
        "resolved_instances": 1,
        "unresolved_instances": 1,
        "empty_patch_instances": 1,
        "error_instances": 2,
        "completed_ids": ["a__b-1", "a__b-2"],
        "incomplete_ids": [],
        "empty_patch_ids": ["a__b-3"],
        "submitted_ids": IDS,
        "resolved_ids": ["a__b-1"],
        "unresolved_ids": ["a__b-2"],
        "infra_failure_ids": [],
        "ambiguous_failure_ids": [],
        "failure_reasons": {},
        "error_ids": ["a__b-4", "a__b-5"],
        "schema_version": 2,
    }
    return {**base, **changes}


def write_log(logs: Path, instance_id: str, text: str) -> None:
    folder = logs / "longhaul-full" / instance_id
    folder.mkdir(parents=True)
    (folder / "run_instance.log").write_text(text, encoding="utf-8")


# ---------- the verdict of each task ----------
def test_a_resolved_instance_is_resolved() -> None:
    found = report.verdicts(harness_report(), ["a__b-1"])

    assert found["a__b-1"].resolved is True
    assert "resolved" in found["a__b-1"].reason


def test_an_unresolved_instance_is_not_resolved_and_the_reason_says_the_tests_ran() -> None:
    found = report.verdicts(harness_report(), ["a__b-2"])

    assert found["a__b-2"].resolved is False
    assert "tests" in found["a__b-2"].reason


def test_an_empty_patch_is_not_resolved() -> None:
    found = report.verdicts(harness_report(), ["a__b-3"])

    assert found["a__b-3"].resolved is False
    assert "empty" in found["a__b-3"].reason


def test_an_error_whose_log_says_the_patch_did_not_apply_is_not_resolved(tmp_path: Path) -> None:
    write_log(tmp_path, "a__b-4", "...\n>>>>> Patch Apply Failed:\nerror: patch failed\n")

    found = report.verdicts(harness_report(), ["a__b-4"], logs_dir=tmp_path)

    assert found["a__b-4"].resolved is False
    assert "apply" in found["a__b-4"].reason


def test_an_error_without_that_mark_is_ungraded_because_the_harness_failed(tmp_path: Path) -> None:
    write_log(tmp_path, "a__b-5", "Error creating container: image not found\n")

    found = report.verdicts(harness_report(), ["a__b-5"], logs_dir=tmp_path)

    assert found["a__b-5"].resolved is None
    assert "run_instance.log" in found["a__b-5"].reason


def test_an_error_with_no_log_at_all_is_ungraded() -> None:
    found = report.verdicts(harness_report(), ["a__b-5"])

    assert found["a__b-5"].resolved is None


def test_a_task_the_report_does_not_mention_is_ungraded() -> None:
    found = report.verdicts(harness_report(), ["a__b-6"])

    assert found["a__b-6"].resolved is None
    assert "not in the report" in found["a__b-6"].reason


def test_an_instance_that_the_harness_flagged_as_an_infrastructure_failure_says_so() -> None:
    harness = harness_report(
        unresolved_ids=["a__b-2"],
        infra_failure_ids=["a__b-2"],
        failure_reasons={"a__b-2": "the test run could not start"},
    )

    found = report.verdicts(harness, ["a__b-2"])

    # The harness counts it as unresolved, so the result does too, and the reason keeps the flag.
    assert found["a__b-2"].resolved is False
    assert "infrastructure" in found["a__b-2"].reason
    assert "the test run could not start" in found["a__b-2"].reason


def test_a_report_of_another_shape_is_refused() -> None:
    with pytest.raises(ValueError, match="resolved_ids"):
        report.verdicts({"schema_version": 2}, ["a__b-1"])


# ---------- the report file ----------
def test_the_report_file_is_found_by_the_run_id(tmp_path: Path) -> None:
    (tmp_path / "longhaul-full-claude-sonnet-5-5.r1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "gold.other.json").write_text("{}", encoding="utf-8")

    assert (
        report.find_report(tmp_path, "r1") == tmp_path / "longhaul-full-claude-sonnet-5-5.r1.json"
    )


def test_no_report_file_is_an_error_that_names_the_folder(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=str(tmp_path)):
        report.find_report(tmp_path, "r1")


def test_two_report_files_for_one_run_id_are_an_error(tmp_path: Path) -> None:
    (tmp_path / "m1.r1.json").write_text("{}", encoding="utf-8")
    (tmp_path / "m2.r1.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="more than one"):
        report.find_report(tmp_path, "r1")


# ---------- writing the verdicts into the result files ----------
def make_run(
    folder: Path,
    ids: list[str],
    *,
    task_set: str = "swebench",
    recorded: dict[str, tuple[str, bool | None, str]] | None = None,
) -> None:
    """Result files of a run. `recorded` gives (outcome, resolved, grading) for the tasks that the
    runner did not leave as finished and not yet graded."""
    recorded = recorded or {}
    for task_id in ids:
        outcome, resolved, grading = recorded.get(
            task_id, ("pr_opened", None, "not graded yet: run eval/swebench/grade.sh")
        )
        results.write_result(
            folder,
            TaskResult(
                run_id=folder.name,
                task_id=task_id,
                workflow_id=f"lh-eval-{folder.name}-{task_id}",
                task_set=task_set,  # type: ignore[arg-type]
                config="full",
                config_hash="a" * 64,
                seed=42,
                model="claude-sonnet-5-5",
                git_sha="f" * 40,
                provider=None,
                scripted=False,
                outcome=outcome,
                outcome_summary="done",
                resolved=resolved,
                grading=grading,
                cost_usd=0.25,
                tokens=Tokens(),
                cache_ratio=0.0,
                started_at=START,
                ended_at=START,
                wall_time_s=60.0,
                turns=4,
            ),
        )
    results.write_summary(
        folder,
        results.summarize(results.read_results(folder), started_at=START, ended_at=START),
    )


def test_the_verdicts_go_into_the_result_files_and_the_summary_is_rebuilt(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    make_run(run_dir, IDS[:3])
    (tmp_path / "x.r1.json").write_text(
        json.dumps(
            harness_report(
                resolved_ids=["a__b-1"],
                unresolved_ids=["a__b-2"],
                empty_patch_ids=["a__b-3"],
                error_ids=[],
            )
        ),
        encoding="utf-8",
    )

    summary = report.apply(run_dir, tmp_path / "x.r1.json")

    by_id = {r.task_id: r for r in results.read_results(run_dir)}
    assert by_id["a__b-1"].resolved is True
    assert by_id["a__b-2"].resolved is False
    assert by_id["a__b-3"].resolved is False
    assert by_id["a__b-1"].grading != "not graded yet: run eval/swebench/grade.sh"
    assert (summary.attempted, summary.resolved, summary.ungraded) == (3, 1, 0)
    assert summary.cost_usd == 0.75
    on_disk = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert on_disk["resolved"] == 1
    # The start and the end of the run are the ones the runner wrote, not the time of grading.
    assert datetime.fromisoformat(on_disk["started_at"]) == START


def test_every_other_field_of_a_result_stays_as_it_was(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    make_run(run_dir, ["a__b-1"])
    before = results.read_results(run_dir)[0]
    path = tmp_path / "x.r1.json"
    path.write_text(
        json.dumps(
            harness_report(
                resolved_ids=["a__b-1"], error_ids=[], unresolved_ids=[], empty_patch_ids=[]
            )
        ),
        encoding="utf-8",
    )

    report.apply(run_dir, path)

    after = results.read_results(run_dir)[0]
    assert after.model_dump(exclude={"resolved", "grading"}) == before.model_dump(
        exclude={"resolved", "grading"}
    )


def test_a_run_graded_twice_ends_with_the_second_verdict(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    make_run(run_dir, ["a__b-1"])
    path = tmp_path / "x.r1.json"
    for ids in (["a__b-1"], []):
        path.write_text(
            json.dumps(
                harness_report(
                    resolved_ids=ids,
                    unresolved_ids=[],
                    empty_patch_ids=[],
                    error_ids=[] if ids else ["a__b-1"],
                )
            ),
            encoding="utf-8",
        )
        report.apply(run_dir, path)

    assert results.read_results(run_dir)[0].resolved is None


def test_a_task_that_delivered_no_patch_keeps_the_verdict_the_runner_wrote(tmp_path: Path) -> None:
    # The predictions give every task a row, with an empty patch for the ones that delivered
    # nothing, so the harness lists them all as empty patches. The runner knows better why.
    run_dir = tmp_path / "r1"
    failed = "not graded: the runner failed: ImageError: pulling x failed"
    stuck = "not graded: the task ended as stuck"
    make_run(
        run_dir,
        IDS[:3],
        recorded={"a__b-2": ("error", None, failed), "a__b-3": ("stuck", False, stuck)},
    )
    (tmp_path / "x.r1.json").write_text(
        json.dumps(
            harness_report(
                resolved_ids=["a__b-1"],
                unresolved_ids=[],
                empty_patch_ids=["a__b-2", "a__b-3"],
                error_ids=[],
            )
        ),
        encoding="utf-8",
    )

    summary = report.apply(run_dir, tmp_path / "x.r1.json")

    by_id = {r.task_id: r for r in results.read_results(run_dir)}
    assert by_id["a__b-1"].resolved is True
    assert (by_id["a__b-2"].resolved, by_id["a__b-2"].grading) == (None, failed)
    assert (by_id["a__b-3"].resolved, by_id["a__b-3"].grading) == (False, stuck)
    # The runner failure is still counted as not graded, and not as an agent that did nothing.
    assert (summary.attempted, summary.resolved, summary.ungraded) == (3, 1, 1)


def test_results_of_the_custom_set_are_not_touched(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    make_run(run_dir, ["a__b-1"], task_set="custom")
    path = tmp_path / "x.r1.json"
    path.write_text(json.dumps(harness_report()), encoding="utf-8")

    with pytest.raises(ValueError, match="swebench"):
        report.apply(run_dir, path)


def test_an_empty_run_folder_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "r1").mkdir()
    path = tmp_path / "x.r1.json"
    path.write_text(json.dumps(harness_report()), encoding="utf-8")

    with pytest.raises(ValueError, match="no result files"):
        report.apply(tmp_path / "r1", path)
