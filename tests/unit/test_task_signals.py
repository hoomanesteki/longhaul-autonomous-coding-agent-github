"""TaskWorkflow's signal handlers and status query, called as plain methods.

In production the workflow runs inside Temporal's sandbox, but its handlers only change fields
of self.state, so they can run here without a server. They are the part of the workflow that
tests/integration/test_task_workflow.py cannot see: that file watches what the workflow does,
and the budget and plan fields have no effect to watch yet.
"""

from datetime import UTC, datetime

import pytest
from temporalio import workflow

from longhaul.agent import hints
from longhaul.contracts import (
    CarryState,
    Outcome,
    OutcomeKind,
    Phase,
    Plan,
    PlanStep,
    RepoSettings,
    TaskSpec,
    TaskStatus,
)
from longhaul.durable import acts
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
        "report_status",  # LH-308: the answer to /longhaul status
    }
    assert set(definition.queries) == {"status"}


def test_a_hint_is_stored_with_its_author(wf: TaskWorkflow, state: CarryState) -> None:
    wf.hint("alice", "try pytest -x")
    wf.hint("bob", "mind the cache")
    assert state.hints == ["alice: try pytest -x", "bob: mind the cache"]


def test_a_huge_hint_is_cut_when_it_arrives(wf: TaskWorkflow, state: CarryState) -> None:
    # The hints stay in the state and go to every llm_turn, so each one has a size limit (LH-506).
    wf.hint("alice", "x" * 300_000)
    (stored,) = state.hints
    assert stored.startswith("alice: " + "x" * 100)
    assert stored.endswith(hints.CUT_NOTE)
    assert len(stored.encode()) < 3_000


def test_a_long_name_is_cut_too(wf: TaskWorkflow, state: CarryState) -> None:
    wf.hint("n" * 500, "go on")
    assert state.hints == [f"{'n' * hints.MAX_AUTHOR_CHARS}: go on"]


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


def test_approving_a_plan_stores_the_decision(wf: TaskWorkflow, state: CarryState) -> None:
    wf.approve_plan("bob")
    assert wf.plan_decision == PlanDecision(approved=True, by="bob")
    # Only the gate sets the flag, when it takes the decision (tests/unit/test_plan_gate_state.py).
    assert state.plan_approved is False


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


def test_a_run_that_posted_no_status_comment_leaves_the_memo_to_the_next_run(
    wf: TaskWorkflow,
) -> None:
    # The memo of the run already holds the count of an earlier run, and None carries it as it is.
    assert wf._memo_to_carry() is None


# ---------- the plan in status() (LH-503) ----------
def two_steps(*statuses: str) -> Plan:
    steps = [
        PlanStep(
            id=f"s{n}",
            description=f"Change module {n}",
            done_when="the tests pass",
            status=status,  # type: ignore[arg-type]
        )
        for n, status in enumerate(statuses, start=1)
    ]
    return Plan(summary="plan", steps=steps, acceptance_checks=["pytest -q"])


@pytest.mark.phase5
def test_status_shows_the_plan_with_the_status_of_each_step(
    wf: TaskWorkflow, state: CarryState
) -> None:
    state.phase = Phase.CODING
    state.plan_version = 1
    state.plan = two_steps("done", "doing")

    shown = wf.status()

    assert isinstance(shown, acts.PlannedStatus)
    assert shown.plan is not None
    assert [s.status for s in shown.plan.steps] == ["done", "doing"]
    # The rest is the status it always was.
    assert TaskStatus(**shown.model_dump(exclude={"plan"})) == TaskStatus(
        task_id="lh-acme-calc-42",
        phase=Phase.CODING,
        turn_index=0,
        cost_usd=0.0,
        last_checkpoint=None,
        plan_version=1,
    )


@pytest.mark.phase5
def test_status_is_the_plain_task_status_for_a_task_without_a_plan(wf: TaskWorkflow) -> None:
    assert type(wf.status()) is TaskStatus


@pytest.mark.phase8
@pytest.mark.parametrize("by", ["alice", "Console", "console "])
def test_a_signal_from_anyone_but_the_console_starts_no_issue_comment(
    wf: TaskWorkflow, by: str
) -> None:
    # The comment of a person on the issue is on the issue already. The checks run before the
    # workflow memo is read, so this works outside a workflow.
    wf.hint(by, "go on")
    wf.add_budget(by, 1.0, 1)
    wf.approve_plan(by)
    wf.reject_plan(by, "no")

    assert wf.mirrors == set()
    assert wf.console_posts is None
    assert wf._memo_to_carry() is None
