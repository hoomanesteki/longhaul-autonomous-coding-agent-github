"""TaskWorkflow's signal handlers and status query, called as plain methods.

In production the workflow runs inside Temporal's sandbox, but its handlers only change fields
of self.state, so they can run here without a server. They are the part of the workflow that
tests/integration/test_task_workflow.py cannot see: that file watches what the workflow does,
and the budget and plan fields have no effect to watch yet.
"""

from datetime import UTC, datetime

import pytest
from temporalio import workflow

from longhaul.contracts import (
    CarryState,
    Outcome,
    OutcomeKind,
    Phase,
    RepoSettings,
    TaskSpec,
    TaskStatus,
)
from longhaul.durable.workflows.task import (
    CancelRequest,
    PlanDecision,
    TaskWorkflow,
    cancel_summary,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


@pytest.fixture
def state() -> CarryState:
    spec = TaskSpec(
        task_id="lh-acme-calc-42",
        repo="acme/calc",
        issue_number=42,
        title="add() subtracts",
        body="add(2, 3) returns -1",
        settings=RepoSettings(repo="acme/calc"),
    )
    return CarryState(
        spec=spec, phase=Phase.QUEUED, started_at=datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    )


@pytest.fixture
def wf(state: CarryState) -> TaskWorkflow:
    return TaskWorkflow(state)


def test_the_workflow_keeps_the_state_it_was_started_with(
    wf: TaskWorkflow, state: CarryState
) -> None:
    assert wf.state is state
    assert wf.outcome is None
    assert wf.cancel_request is None
    assert wf.plan_decision is None


def test_the_signals_and_the_query_have_the_names_of_section_3_7() -> None:
    # Clients send signals by name, so renaming a method would break them without a type error.
    definition = workflow._Definition.must_from_class(TaskWorkflow)
    assert definition.name == "TaskWorkflow"
    assert set(definition.signals) == {
        "cancel",
        "hint",
        "add_budget",
        "approve_plan",
        "reject_plan",
    }
    assert set(definition.queries) == {"status"}


def test_a_hint_is_stored_with_its_author(wf: TaskWorkflow, state: CarryState) -> None:
    wf.hint("alice", "try pytest -x")
    wf.hint("bob", "mind the cache")
    assert state.hints == ["alice: try pytest -x", "bob: mind the cache"]


def test_budget_top_ups_add_up(wf: TaskWorkflow, state: CarryState) -> None:
    wf.add_budget("alice", 2.5, 10)
    wf.add_budget("bob", 1.0, 5)
    assert state.extra_budget_usd == pytest.approx(3.5)
    assert state.extra_turns == 15


def test_a_negative_top_up_cannot_lower_a_budget(wf: TaskWorkflow, state: CarryState) -> None:
    wf.add_budget("alice", -5.0, -3)
    assert (state.extra_budget_usd, state.extra_turns) == (0.0, 0)
    # Each amount is judged on its own: the valid half still counts.
    wf.add_budget("alice", 2.0, -1)
    assert (state.extra_budget_usd, state.extra_turns) == (2.0, 0)


def test_approving_a_plan_stores_the_flag_and_the_decision(
    wf: TaskWorkflow, state: CarryState
) -> None:
    wf.approve_plan("bob")
    assert state.plan_approved is True
    assert wf.plan_decision == PlanDecision(approved=True, by="bob")


def test_rejecting_a_plan_stores_the_feedback(wf: TaskWorkflow, state: CarryState) -> None:
    wf.approve_plan("bob")
    wf.reject_plan("carol", "split the change in two")
    assert state.plan_approved is False
    assert wf.plan_decision == PlanDecision(
        approved=False, by="carol", feedback="split the change in two"
    )


def test_the_first_cancel_wins(wf: TaskWorkflow) -> None:
    wf.cancel("alice", "changed my mind")
    wf.cancel("bob", "me too")
    assert wf.cancel_request == CancelRequest(by="alice", reason="changed my mind")


def test_status_reports_the_state(wf: TaskWorkflow, state: CarryState) -> None:
    state.phase = Phase.CODING
    state.turn_index = 4
    state.cost_usd = 0.75
    state.head_sha = "c" * 40
    state.plan_version = 2
    assert wf.status() == TaskStatus(
        task_id="lh-acme-calc-42",
        phase=Phase.CODING,
        turn_index=4,
        cost_usd=0.75,
        last_checkpoint="c" * 40,
        plan_version=2,
        paused_reason=None,
        outcome=None,
    )


def test_status_has_no_plan_version_until_there_is_a_plan(wf: TaskWorkflow) -> None:
    assert wf.status().plan_version is None


def test_status_carries_the_outcome_once_there_is_one(wf: TaskWorkflow) -> None:
    wf.outcome = Outcome(kind=OutcomeKind.PR_OPENED, summary="fixed add()")
    assert wf.status().outcome == wf.outcome


@pytest.mark.parametrize(
    ("by", "reason", "expected"),
    [
        ("alice", "changed my mind", "cancelled by alice: changed my mind"),
        ("alice", "", "cancelled by alice"),
        ("alice", "   ", "cancelled by alice"),
        ("console", " wrong repo \n", "cancelled by console: wrong repo"),
    ],
)
def test_cancel_summary_names_who_and_why(by: str, reason: str, expected: str) -> None:
    assert cancel_summary(by, reason) == expected
