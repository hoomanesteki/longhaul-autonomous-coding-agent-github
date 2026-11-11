"""The no-op activities that keep the log of a test harness clean (tests/fakes/stand_ins.py)."""

from __future__ import annotations

import pytest
from temporalio import activity
from tests.fakes.stand_ins import no_run_report, no_task_row, with_stand_ins

from longhaul.durable import acts
from longhaul.durable.workflows.poller import PollerWorkflow
from longhaul.durable.workflows.task import TaskWorkflow

pytestmark = [pytest.mark.unit, pytest.mark.phase8]


@activity.defn(name=acts.RECORD_TASK_STATE.name)
async def own_row(inp: acts.RecordTaskStateInput) -> None:
    """A worker's own record_task_state."""


def test_a_worker_of_the_task_workflow_gets_both_stand_ins() -> None:
    assert with_stand_ins([TaskWorkflow], []) == [no_task_row, no_run_report]


def test_a_worker_keeps_its_own_activity_and_gets_the_other_stand_in() -> None:
    assert with_stand_ins([TaskWorkflow], [own_row]) == [own_row, no_run_report]


def test_a_worker_of_another_workflow_is_left_alone() -> None:
    assert with_stand_ins([PollerWorkflow], []) == []
