"""The record_task_state activity: the row of a task, kept current for the console (LH-891).

The workflow cannot write to Postgres, so it asks for each change of the row through this
activity. The body is one call to the task store.
"""

from datetime import UTC, datetime

import pytest
from longhaul.durable.activities.task_row import TaskRowActivities
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.contracts import OutcomeKind, Phase, RepoSettings
from longhaul.db.tasks import MemoryTaskStore
from longhaul.durable import acts
from longhaul.durable.payload_guard import DEFAULT_LIMIT

pytestmark = [pytest.mark.unit, pytest.mark.phase8]

TASK = "lh-acme-calc-42"
NOON = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def given(**changes: object) -> acts.RecordTaskStateInput:
    values: dict[str, object] = {
        "task_id": TASK,
        "repo": "acme/calc",
        "issue_number": 42,
        "title": "add() subtracts",
        "settings": RepoSettings(repo="acme/calc", test_command="pytest -q"),
        "phase": Phase.CODING,
        "turns": 4,
        "cost_usd": 0.75,
    }
    return acts.RecordTaskStateInput(**{**values, **changes})  # type: ignore[arg-type]


async def record(store: MemoryTaskStore, inp: acts.RecordTaskStateInput) -> None:
    await ActivityEnvironment().run(TaskRowActivities(store).record_task_state, inp)


async def test_the_row_is_written_with_the_phase_the_turns_and_the_cost() -> None:
    store = MemoryTaskStore()

    await record(store, given())

    row = store.rows[TASK]
    assert (row.repo, row.issue_number, row.title) == ("acme/calc", 42, "add() subtracts")
    assert (row.status, row.phase, row.turns, row.cost_usd) == ("active", "coding", 4, 0.75)
    assert row.settings["test_command"] == "pytest -q"


async def test_the_end_of_a_task_carries_the_outcome_the_url_and_the_time() -> None:
    store = MemoryTaskStore()

    await record(
        store,
        given(
            phase=Phase.DONE,
            outcome=OutcomeKind.PR_OPENED,
            pr_url="https://github.com/acme/calc/pull/9",
            ended_at=NOON,
        ),
    )

    row = store.rows[TASK]
    assert (row.status, row.phase, row.outcome) == ("done", "done", "pr_opened")
    assert (row.pr_url, row.ended_at) == ("https://github.com/acme/calc/pull/9", NOON)


@pytest.mark.parametrize(
    ("phase", "kind", "status"),
    [
        (Phase.CANCELLED, OutcomeKind.CANCELLED, "cancelled"),
        (Phase.FAILED, OutcomeKind.ERROR, "failed"),
        (Phase.FAILED, OutcomeKind.STUCK, "failed"),
        (Phase.FAILED, OutcomeKind.BUDGET_EXHAUSTED, "failed"),
    ],
)
async def test_a_task_that_ended_has_the_status_of_how_it_ended(
    phase: Phase, kind: OutcomeKind, status: str
) -> None:
    store = MemoryTaskStore()

    await record(store, given(phase=phase, outcome=kind, ended_at=NOON))

    assert store.rows[TASK].status == status


async def test_an_oversized_input_is_refused_for_good() -> None:
    store = MemoryTaskStore()
    big = given(pr_url="x" * (DEFAULT_LIMIT + 1))

    with pytest.raises(ApplicationError) as caught:
        await record(store, big)

    assert caught.value.non_retryable
    assert store.rows == {}


def test_the_activity_is_on_every_worker_and_retried_like_the_other_database_writes() -> None:
    act = acts.RECORD_TASK_STATE
    assert act in acts.ALL and act not in acts.GITHUB
    assert not act.local
    assert act.retry.maximum_attempts == acts.STORE_PLAN.retry.maximum_attempts
    never = act.retry.non_retryable_error_types or []
    # A worker that predates the activity cannot run it, and asking again cannot change that.
    assert "NotFoundError" in never


def test_the_activity_registers_under_its_name() -> None:
    registered = TaskRowActivities(MemoryTaskStore()).activities
    assert [fn.__temporal_activity_definition.name for fn in registered] == [  # type: ignore[attr-defined]
        acts.RECORD_TASK_STATE.name
    ]
