"""Write the verdicts of the official SWE-bench harness into the result files of a run (LH-902).

    uv run python -m eval.swebench.report RUN_DIR --work-dir DIR

`grade.sh` runs `python -m swebench.harness.run_evaluation` in a work folder. The harness leaves
a report there, `<model_name_or_path>.<run_id>.json`, and a log folder per instance. This module
reads the report and sets `resolved` and `grading` in each `<task_id>.json` of the run, then
rebuilds `summary.json` from the result files.

The harness sorts every instance of the dataset into lists. A task of the run lands in one:

* `resolved_ids`: resolved. Every FAIL_TO_PASS and PASS_TO_PASS test passed.
* `unresolved_ids`: not resolved. The tests ran and some did not pass.
* `empty_patch_ids`: not resolved. There was no patch, and the harness starts no container.
* `error_ids`: not resolved when the instance log says the patch did not apply, else not graded
  (null), because the harness itself failed.

A task that is in none of the lists is left not graded: the report does not know it.

Only a task that ended as `pr_opened` gets a verdict. The predictions give every task a row, with
an empty patch for the ones that delivered nothing, so the harness lists them all as empty
patches. The runner knows why such a task has no patch: the agent got stuck, ran out of budget or
was cancelled (not resolved), or the runner failed before the agent could work (not graded). The
harness knows none of that, so those result files stay as the runner wrote them.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

from eval import results
from longhaul.contracts import OutcomeKind

# The lists of the report that this module reads. An integration test checks that the installed
# swebench still writes them.
REPORT_FIELDS = (
    "resolved_ids",
    "unresolved_ids",
    "empty_patch_ids",
    "error_ids",
    "submitted_ids",
)
PATCH_APPLY_FAILED = ">>>>> Patch Apply Failed"
INSTANCE_LOG = "run_instance.log"


class Verdict(NamedTuple):
    resolved: bool | None
    reason: str


def _apply_failed(instance_id: str, logs_dir: Path | None) -> bool:
    if logs_dir is None:
        return False
    for log in logs_dir.glob(f"*/{instance_id}/{INSTANCE_LOG}"):
        if PATCH_APPLY_FAILED in log.read_text(encoding="utf-8", errors="replace"):
            return True
    return False


def verdicts(
    report: Mapping[str, Any], task_ids: Sequence[str], logs_dir: Path | None = None
) -> dict[str, Verdict]:
    """The verdict of each task id. `logs_dir` holds <model>/<instance>/run_instance.log."""
    for field in REPORT_FIELDS:
        if field not in report:
            raise ValueError(f"the report of the harness has no {field}; is it a swebench report?")
    flags = {
        **dict.fromkeys(report.get("ambiguous_failure_ids", []), "an ambiguous failure"),
        **dict.fromkeys(report.get("infra_failure_ids", []), "an infrastructure failure"),
    }
    reasons = report.get("failure_reasons", {})
    found: dict[str, Verdict] = {}
    for task_id in task_ids:
        found[task_id] = _verdict(task_id, report, flags, reasons, logs_dir)
    return found


def _verdict(
    task_id: str,
    report: Mapping[str, Any],
    flags: Mapping[str, str],
    reasons: Mapping[str, str],
    logs_dir: Path | None,
) -> Verdict:
    if task_id in report["resolved_ids"]:
        return Verdict(True, "resolved: every FAIL_TO_PASS and PASS_TO_PASS test passed")
    if task_id in report["unresolved_ids"]:
        text = "the harness ran the tests and not all of them passed"
        if task_id in flags:
            why = reasons.get(task_id)
            text += f"; it flags {flags[task_id]}" + (f": {why}" if why else "")
        return Verdict(False, text)
    if task_id in report["empty_patch_ids"]:
        return Verdict(False, "empty patch: the agent delivered no change")
    if task_id in report["error_ids"]:
        if _apply_failed(task_id, logs_dir):
            return Verdict(False, "the patch does not apply to the repo of the instance")
        return Verdict(None, f"the harness did not grade it; see {INSTANCE_LOG} of the instance")
    return Verdict(None, "the task is not in the report of the harness")


def find_report(work_dir: Path, run_id: str) -> Path:
    """The report file the harness wrote for `run_id` in `work_dir`."""
    found = sorted(work_dir.glob(f"*.{run_id}.json"))
    if not found:
        raise FileNotFoundError(f"no report for run {run_id} in {work_dir}")
    if len(found) > 1:
        names = ", ".join(p.name for p in found)
        raise ValueError(f"more than one report for run {run_id} in {work_dir}: {names}")
    return found[0]


def _run_times(run_dir: Path, found: Sequence[results.TaskResult]) -> tuple[datetime, datetime]:
    """When the run began and ended, as the runner wrote it. Grading does not move them."""
    try:
        old = results.Summary.model_validate_json(
            (run_dir / results.SUMMARY_FILE).read_text(encoding="utf-8")
        )
        return old.started_at, old.ended_at
    except (OSError, ValueError):
        return min(r.started_at for r in found), max(r.ended_at for r in found)


def apply(run_dir: Path, report_path: Path, logs_dir: Path | None = None) -> results.Summary:
    """Set the verdicts in the result files of `run_dir` and rebuild its summary.

    Only the tasks that ended as `pr_opened` are graded. The others keep what the runner wrote.
    """
    found = results.read_results(run_dir)
    if not found:
        raise ValueError(f"no result files in {run_dir}")
    for result in found:
        if result.task_set != "swebench":
            raise ValueError(f"{run_dir} is a run of the {result.task_set} set, not of swebench")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    delivered = [r.task_id for r in found if r.outcome == OutcomeKind.PR_OPENED.value]
    decided = verdicts(report, delivered, logs_dir)
    for task_id, verdict in decided.items():
        results.set_verdict(run_dir, task_id, resolved=verdict.resolved, grading=verdict.reason)
    started_at, ended_at = _run_times(run_dir, found)
    summary = results.summarize(
        results.read_results(run_dir), started_at=started_at, ended_at=ended_at
    )
    results.write_summary(run_dir, summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Put the harness verdicts into a run folder.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument(
        "--work-dir", type=Path, required=True, help="where grade.sh ran the harness"
    )
    args = parser.parse_args(argv)
    run_id = args.run_dir.resolve().name
    summary = apply(
        args.run_dir,
        find_report(args.work_dir, run_id),
        logs_dir=args.work_dir / "logs" / "run_evaluation" / run_id,
    )
    print(
        f"run {summary.run_id}: {summary.resolved} of {summary.attempted} resolved, "
        f"{summary.ungraded} not graded ({args.run_dir / results.SUMMARY_FILE})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
