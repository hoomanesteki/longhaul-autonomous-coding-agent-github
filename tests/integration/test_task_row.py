"""The row of a task in `tasks`, kept current by its workflow (LH-891).

The poller writes the row once, at pickup, and the console lists tasks from it. Before this story
nothing wrote it again, so the list showed every task as queued with no turns and no cost. These
tests run the workflow on the time skipping Temporal server with the real record_task_state
activity and check the row at each stage, then check the SQL upsert behind it on a scratch
database.

The workflow side reuses the worker of test_pause_flow.py (fake sandbox, scripted provider, real
llm_turn, tool_exec and record_event). A task there is a local one, which the poller never saw, so
the first write has to create the row.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from tests.integration import test_pause_flow as pause_tests
from tests.integration import test_run_report as report_tests
from tests.integration.test_pause_flow import (
    FINISH,
    PLAN,
    SUMMARY,
    SUMMARY_STEP,
    VERDICT,
    World,
    light,
    paused,
    result_of,
    send_hint,
    tool,
    turn,
    wait_until,
    write_script,
)

from longhaul.contracts import OutcomeKind, RepoSettings, TaskSpec
from longhaul.db.models import Task
from longhaul.db.tasks import CommentCursor, MemoryTaskStore, SqlTaskStore, TaskState
from longhaul.durable import acts
from longhaul.durable.client import approve_plan, start_task
from longhaul.durable.workflows.task import TaskWorkflow

pytestmark = [pytest.mark.integration, pytest.mark.phase8]

env = pause_tests.env
database = report_tests.database
sessions = report_tests.sessions

Sessions = async_sessionmaker[AsyncSession]
NOON = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def gated(spec: TaskSpec) -> TaskSpec:
    """The same task on a repo that wants a decision on the plan."""
    settings = spec.settings.model_copy(update={"plan_approval_required": True})
    return spec.model_copy(update={"settings": settings})


def planned_and_asking() -> list[dict[str, Any]]:
    return [
        turn("planner", 0, tool("submit_plan", **PLAN)),
        turn("coder", 0, tool("ask_human", question="Should lookup accept a list of skus?")),
        {
            "match": {"role": "coder", "turn_index": 1},
            "respond": {"tool_calls": [tool("submit_for_review", summary=SUMMARY)]},
        },
        turn("verifier", 0, VERDICT),
        SUMMARY_STEP,
    ]


async def row_in(world: World, task_id: str, phase: str) -> None:
    def there() -> bool:
        row = world.tasks.rows.get(task_id)
        return row is not None and row.phase == phase

    await wait_until(there, f"the row in phase {phase}")


def collapsed(world: World) -> list[str]:
    """The phases written, in order, with a repeat of the phase just before it left out."""
    phases: list[str] = []
    for written in world.tasks.written:
        if not phases or phases[-1] != written.phase:
            phases.append(written.phase)
    return phases


# ---------- the workflow keeps the row ----------
async def test_the_row_follows_a_task_through_a_plan_a_pause_and_a_finish(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with light(env, write_script(tmp_path, planned_and_asking())) as world:
        spec = gated(world.spec())
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)

        # The task came from `longhaul run`, so no poller wrote a row. The first write makes it.
        await row_in(world, spec.task_id, "awaiting_plan_approval")
        row = world.tasks.rows[spec.task_id]
        assert (row.status, row.repo, row.issue_number) == ("active", spec.repo, None)
        assert row.title == spec.title
        # The planner's turn is a turn of the task.
        assert row.turns == (await handle.query(TaskWorkflow.status)).turn_index == 1
        assert row.settings["plan_approval_required"] is True
        started = row.started_at

        await approve_plan(env.client, spec.task_id, by="maintainer")
        await paused(world, handle)
        await row_in(world, spec.task_id, "paused")
        live = await handle.query(TaskWorkflow.status)
        assert (row.status, row.turns) == ("active", 2)
        assert row.turns == live.turn_index
        # The cost includes the question for the person, which is written just after the phase.
        await wait_until(
            lambda: row.cost_usd == pytest.approx(live.cost_usd), "the cost of the question"
        )
        assert row.cost_usd > 0
        assert row.outcome is None and row.ended_at is None

        await send_hint(handle, "yes, a list of skus")
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert (row.status, row.phase, row.outcome) == ("done", "done", "pr_opened")
    assert row.ended_at is not None and row.ended_at >= started
    assert (row.turns, row.cost_usd) == (final.turn_index, pytest.approx(final.cost_usd))
    assert row.started_at == started
    assert collapsed(world) == [
        "preparing",
        "planning",
        "awaiting_plan_approval",
        "coding",
        "paused",
        "coding",
        "verifying",
        "done",
    ]


async def test_a_task_the_poller_picked_up_keeps_its_cursor_settings_and_title(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    tasks = MemoryTaskStore()
    async with light(env, write_script(tmp_path, [turn("coder", 0, FINISH)]), tasks=tasks) as world:
        spec = world.spec()
        await tasks.record_pickup(spec, cursor=CommentCursor(NOON, 77))
        row = tasks.rows[spec.task_id]
        started = row.started_at
        handle = await start_task(env.client, spec, task_queue=world.queue)
        await result_of(handle)

    assert (row.status, row.phase, row.outcome) == ("done", "done", "pr_opened")
    assert row.cursor == CommentCursor(NOON, 77)
    assert row.title == spec.title
    assert row.settings == spec.settings.model_dump(mode="json")
    assert row.started_at == started


async def test_turns_and_cost_do_not_wait_for_the_end_of_the_task(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [turn("coder", i, tool("run_command", command=f"echo {i}")) for i in range(8)]
    steps.append(turn("coder", 8, FINISH))
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await result_of(handle)

    coding = [w for w in world.tasks.written if w.phase == "coding"]
    # A write every few turns while the task codes, so a long task is never far behind.
    assert {w.turns for w in coding} >= {0, 3, 6}
    assert [w.cost_usd for w in coding] == sorted(w.cost_usd for w in coding)
    assert coding[-1].cost_usd > 0


async def test_a_long_title_is_cut_before_it_is_written(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with light(env, write_script(tmp_path, [turn("coder", 0, FINISH)])) as world:
        spec = world.spec().model_copy(update={"title": "t" * 5_000})
        handle = await start_task(env.client, spec, task_queue=world.queue)
        await result_of(handle)

    assert len(world.tasks.rows[spec.task_id].title) == acts.ROW_TITLE_CHARS


class DownStore(MemoryTaskStore):
    """A task store whose database is down."""

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def record_state(self, state: TaskState) -> None:
        self.attempts += 1
        raise ConnectionError("the database is down")


async def test_a_row_that_cannot_be_written_does_not_fail_the_task(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    down = DownStore()
    async with light(env, write_script(tmp_path, [turn("coder", 0, FINISH)]), tasks=down) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    # Each write is tried as often as the other database writes of the workflow are.
    assert down.attempts >= acts.RECORD_TASK_STATE.retry.maximum_attempts
    assert down.rows == {}


async def test_a_cancelled_task_ends_its_row_as_cancelled(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with light(env, write_script(tmp_path, planned_and_asking())) as world:
        spec = gated(world.spec())
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        await row_in(world, spec.task_id, "awaiting_plan_approval")
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "not needed"])
        outcome = await result_of(handle)

    row = world.tasks.rows[spec.task_id]
    assert outcome.kind is OutcomeKind.CANCELLED
    assert (row.status, row.phase, row.outcome) == ("cancelled", "cancelled", "cancelled")
    assert row.ended_at is not None


# ---------- the SQL upsert behind it ----------
def state(task_id: str, **changes: Any) -> TaskState:
    values: dict[str, Any] = {
        "task_id": task_id,
        "repo": "acme/calc",
        "issue_number": 4,
        "title": "add() is wrong",
        "settings": {"repo": "acme/calc", "test_command": "pytest -q"},
        "phase": "coding",
        "turns": 3,
        "cost_usd": 0.5,
    }
    return TaskState(**{**values, **changes})


def spec_for(task_id: str) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repo="acme/calc",
        issue_number=4,
        title="add() is wrong",
        body="",
        settings=RepoSettings(repo="acme/calc", test_command="pytest -q"),
    )


@pytest.fixture
async def store(sessions: Sessions) -> AsyncIterator[SqlTaskStore]:
    yield SqlTaskStore(sessions)


async def stored(sessions: Sessions, task_id: str) -> Task:
    async with sessions() as session:
        row = await session.get(Task, task_id)
    assert row is not None
    return row


async def test_the_sql_store_creates_the_row_of_a_task_nobody_picked_up(
    store: SqlTaskStore, sessions: Sessions
) -> None:
    task_id = f"lh-local-{uuid.uuid4().hex[:8]}"

    await store.record_state(state(task_id, issue_number=None))

    row = await stored(sessions, task_id)
    assert (row.status, row.phase, row.turns, float(row.cost_usd)) == ("active", "coding", 3, 0.5)
    assert row.issue_number is None and row.started_at is not None
    assert row.settings["test_command"] == "pytest -q"


async def test_the_sql_store_updates_the_row_and_leaves_the_cursor_and_the_settings(
    store: SqlTaskStore, sessions: Sessions
) -> None:
    task_id = f"lh-acme-calc-{uuid.uuid4().hex[:8]}"
    await store.record_pickup(spec_for(task_id), cursor=CommentCursor(NOON, 77))
    before = await stored(sessions, task_id)

    await store.record_state(
        state(task_id, title="elsewhere", settings={"repo": "acme/calc"}, phase="paused", turns=5)
    )

    row = await stored(sessions, task_id)
    assert (row.status, row.phase, row.turns) == ("active", "paused", 5)
    assert row.title == "add() is wrong" and row.settings["test_command"] == "pytest -q"
    assert (row.last_comment_at, row.last_comment_id) == (NOON, 77)
    assert row.started_at == before.started_at


async def test_the_sql_store_ends_a_task_and_keeps_the_end_when_an_older_write_arrives(
    store: SqlTaskStore, sessions: Sessions
) -> None:
    task_id = f"lh-acme-calc-{uuid.uuid4().hex[:8]}"
    await store.record_pickup(spec_for(task_id))
    url = "https://github.com/acme/calc/pull/9"

    await store.record_state(
        state(task_id, phase="done", outcome="pr_opened", pr_url=url, ended_at=NOON, turns=7)
    )
    await store.record_state(state(task_id, phase="done", turns=6, cost_usd=0.25))

    row = await stored(sessions, task_id)
    assert (row.status, row.phase, row.outcome, row.pr_url) == ("done", "done", "pr_opened", url)
    assert row.ended_at == NOON
    assert (row.turns, float(row.cost_usd)) == (7, 0.5)


async def test_the_sql_store_keeps_the_cost_the_ledger_summed(
    store: SqlTaskStore, sessions: Sessions
) -> None:
    task_id = f"lh-acme-calc-{uuid.uuid4().hex[:8]}"
    await store.record_pickup(spec_for(task_id))
    async with sessions.begin() as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.cost_usd = task.cost_usd + 2  # type: ignore[assignment]

    await store.record_state(state(task_id, cost_usd=0.5))

    assert float((await stored(sessions, task_id)).cost_usd) == 2.0


async def test_a_pickup_after_the_first_state_still_stores_the_cursor(
    store: SqlTaskStore, sessions: Sessions
) -> None:
    # The poller starts the workflow before it writes the row, so the workflow can write first.
    task_id = f"lh-acme-calc-{uuid.uuid4().hex[:8]}"
    await store.record_state(state(task_id))

    await store.record_pickup(spec_for(task_id), cursor=CommentCursor(NOON, 77))
    await store.record_pickup(spec_for(task_id), cursor=CommentCursor(NOON + timedelta(hours=1), 5))

    row = await stored(sessions, task_id)
    assert (row.last_comment_at, row.last_comment_id) == (NOON, 77)
