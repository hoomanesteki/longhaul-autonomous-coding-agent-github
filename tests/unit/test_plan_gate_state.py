"""What the plan gate leaves in the carried state, and what it asks of the issue (LH-502).

The gate takes a decision that is already there without a timer, so `TaskWorkflow._review` runs
here as a plain coroutine, with the three methods that start activities replaced by recorders (the
third writes the row of the task, LH-891). The
wait itself and the replay are in tests/integration/test_plan_gate.py.
"""

from datetime import UTC, datetime

import pytest

from longhaul.contracts import CarryState, Phase, RepoSettings, TaskSpec
from longhaul.durable import acts
from longhaul.durable.workflows.task import PlanDecision, TaskWorkflow
from longhaul.github.lifecycle import LabelStep

pytestmark = [pytest.mark.unit, pytest.mark.phase5]


@pytest.fixture
def state() -> CarryState:
    spec = TaskSpec(
        task_id="lh-acme-calc-42",
        repo="acme/calc",
        issue_number=42,
        title="add() subtracts",
        body="add(2, 3) returns -1",
        settings=RepoSettings(repo="acme/calc", plan_approval_required=True),
    )
    return CarryState(
        spec=spec, phase=Phase.PLANNING, started_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    )


@pytest.fixture
def wf(state: CarryState) -> TaskWorkflow:
    return TaskWorkflow(state)


class Calls:
    """What the gate asked the workflow to run: activity names, and label steps as +/- labels.

    The writes of the row are counted apart, so the lists of activities below stay what they were.
    """

    def __init__(self, wf: TaskWorkflow) -> None:
        self.activities: list[str] = []
        self.labels: list[str] = []
        self.rows = 0
        wf._call = self._call  # type: ignore[method-assign]
        wf._label = self._label  # type: ignore[method-assign]
        wf._save_row = self._save_row  # type: ignore[method-assign]

    async def _save_row(self, ended: object = None) -> None:
        self.rows += 1

    async def _call(self, act: acts.Act, arg: object) -> None:
        self.activities.append(act.name)

    async def _label(self, steps: list[LabelStep]) -> None:
        self.labels.extend(("+" if step.add else "-") + step.label.label for step in steps)


async def test_the_gate_sets_the_flag_for_an_approval(wf: TaskWorkflow, state: CarryState) -> None:
    Calls(wf)
    wf.approve_plan("alice")

    decision = await wf._review(1)

    assert decision == PlanDecision(approved=True, by="alice")
    assert state.plan_approved is True


async def test_the_gate_leaves_the_flag_false_for_a_rejection(
    wf: TaskWorkflow, state: CarryState
) -> None:
    Calls(wf)
    wf.approve_plan("alice")
    wf.reject_plan("bob", "split the change in two")

    decision = await wf._review(1)

    assert decision == PlanDecision(approved=False, by="bob", feedback="split the change in two")
    assert state.plan_approved is False


async def test_a_decision_after_the_gate_does_not_change_the_flag(
    wf: TaskWorkflow, state: CarryState
) -> None:
    """The coder works on the approved plan. A late rejection finds nobody to read it."""
    Calls(wf)
    wf.approve_plan("alice")
    await wf._review(1)

    wf.reject_plan("carol", "Too late.")

    assert state.plan_approved is True


async def test_an_answer_that_is_already_there_asks_nobody(wf: TaskWorkflow) -> None:
    """The request for a decision would stay on the issue after the person has decided."""
    calls = Calls(wf)
    wf.approve_plan("alice")

    await wf._review(1)

    # Only the record of the decision: no label goes on or off, and no comment asks for one.
    assert calls.activities == [acts.DECIDE_PLAN.name]
    assert calls.labels == []
