"""A stand-in for the record_task_state activity, for tests that run TaskWorkflow without one.

The workflow writes the row of its task through `record_task_state` (LH-891). A worker that does
not register the activity answers every write with NotFoundError, and the workflow logs one
warning for each. The row is not what those tests look at, so tests/conftest.py adds this no-op
to a worker that runs TaskWorkflow and has no activity of that name. A test that wants to see the
writes registers its own (the stubs of test_task_workflow, the real one of test_task_row), and
this one stays out of its way.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from temporalio import activity
from temporalio.activity import _Definition  # type: ignore[attr-defined]

from longhaul.durable import acts


@activity.defn(name=acts.RECORD_TASK_STATE.name)
async def no_task_row(inp: acts.RecordTaskStateInput) -> None:
    """Take a write of the row and drop it."""


def _names(activities: Iterable[Any]) -> set[str | None]:
    return {d.name for d in (_Definition.from_callable(a) for a in activities) if d is not None}


def with_task_row(workflows: Iterable[Any], activities: Iterable[Any]) -> list[Any]:
    """The activities a worker should register: its own, plus the no-op when the row is unserved."""
    given = list(activities)
    from longhaul.durable.workflows.task import TaskWorkflow

    if TaskWorkflow in list(workflows) and acts.RECORD_TASK_STATE.name not in _names(given):
        return [*given, no_task_row]
    return given
