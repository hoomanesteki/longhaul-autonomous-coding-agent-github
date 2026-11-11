"""No-op stand-ins for the best-effort activities of TaskWorkflow, for tests that do not serve them.

The workflow writes the row of its task through `record_task_state` (LH-891) and stores the run
report of a task of an issue again at its end through `write_run_report` (LH-991). A worker that
does not register either answers with NotFoundError, and the workflow logs a warning for every
call. Most tests look at neither, so tests/conftest.py adds these to a worker that runs
TaskWorkflow and has no activity of that name. A test that wants to see the calls registers its
own (the stubs of test_task_workflow and test_outcomes, the real ones of test_task_row), and the
stand-in stays out of its way.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Any

from temporalio import activity
from temporalio.activity import _Definition  # type: ignore[attr-defined]

from longhaul.contracts import BlobRef
from longhaul.durable import acts


@activity.defn(name=acts.RECORD_TASK_STATE.name)
async def no_task_row(inp: acts.RecordTaskStateInput) -> None:
    """Take a write of the row and drop it."""


@activity.defn(name=acts.WRITE_RUN_REPORT.name)
async def no_run_report(inp: acts.RunReportInput) -> BlobRef:
    """Answer with the reference of an empty report, and store nothing."""
    return BlobRef(key=f"tasks/{inp.task_id}/report/run-report.md", sha256=EMPTY, size=0)


EMPTY = hashlib.sha256(b"").hexdigest()
STAND_INS: tuple[tuple[str, Callable[..., Any]], ...] = (
    (acts.RECORD_TASK_STATE.name, no_task_row),
    (acts.WRITE_RUN_REPORT.name, no_run_report),
)


def _names(activities: Iterable[Any]) -> set[str | None]:
    return {d.name for d in (_Definition.from_callable(a) for a in activities) if d is not None}


def with_stand_ins(workflows: Iterable[Any], activities: Iterable[Any]) -> list[Any]:
    """The activities a worker should register: its own, plus a no-op for each one it lacks."""
    given = list(activities)
    from longhaul.durable.workflows.task import TaskWorkflow

    if TaskWorkflow not in list(workflows):
        return given
    have = _names(given)
    return [*given, *(fn for name, fn in STAND_INS if name not in have)]
