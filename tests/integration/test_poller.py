"""The poller on a real Temporal server and a real Postgres (LH-307).

The first three tests are the ones the story names. The rest check what only a server can show:
that a schedule run reaches the activity, and that pause, resume and trigger work on it.

* GitHub is FakeGitHub behind the real GitHubClient.
* Temporal is the server of the compose stack (`LONGHAUL_TEMPORAL_ADDRESS`). The time skipping
  test server has no schedules (ADR-0004, appendix section 7), and a worker on the compose server
  is how the other integration tests of the durable layer reach it too.
* Postgres is a throwaway database per test, migrated to head and dropped.

Nothing here shares a name with another test or another developer on the same server. The task
queue of the poller, the queue the tasks are started on, the repo (and so the workflow ids of the
tasks) and the schedule all carry a random suffix. The queue of the tasks has no worker, so the
TaskWorkflows only start; the test terminates them at the end. The schedule is deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from temporalio.client import (
    Client,
    ScheduleActionStartWorkflow,
    ScheduleHandle,
    ScheduleOverlapPolicy,
    WorkflowExecutionStatus,
)
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.contracts import RepoSettings, TaskSpec
from longhaul.db.session import make_engine
from longhaul.db.tasks import SqlTaskStore
from longhaul.durable import acts, schedule
from longhaul.durable.activities.github import PollerActivities
from longhaul.durable.client import connect
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.poller import PollerWorkflow
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.settings import AppConfig, EnvSettings, RepoEntry, get_settings, load_config

pytestmark = [pytest.mark.integration, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_poller_"
# If anything ignored the scratch URL it would fall back to this and fail to connect, instead
# of reaching a real database.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
TOKEN = "ghp_integration_token_0123456789abcdef"
LABEL = "longhaul"
WAIT_S = 30.0


# --- a throwaway database ---------------------------------------------------------------------


@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def query(self, statement: LiteralString) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement).fetchall()

    def execute(self, statement: LiteralString) -> None:
        with psycopg.connect(self.url) as conn:
            conn.execute(statement)


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
    secret = EnvSettings().database_url
    server = make_url(secret.get_secret_value() if secret else LOCAL_DSN)
    admin_dsn = server.set(database="postgres").render_as_string(hide_password=False)
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        url = server.set(database=name).render_as_string(hide_password=False)
        db = Scratch(name, url)
        command.upgrade(db.config(), "head")  # outside the event loop, which alembic needs
        yield db
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


# --- the world --------------------------------------------------------------------------------


@dataclass
class World:
    """Everything a test reaches, under names that only this test uses."""

    fake: FakeGitHub
    client: Client
    scratch: Scratch
    suffix: str
    poller_queue: str
    task_queue: str

    @property
    def repo(self) -> str:
        return f"acme/calc-{self.suffix}"

    @property
    def prefix(self) -> str:
        """The start of every task id of this test's repo."""
        return f"lh-acme-calc-{self.suffix}-"

    def task_id(self, number: int) -> str:
        return f"{self.prefix}{number}"

    def entry(self, repo: str | None = None, *, allowlisted: bool = True) -> RepoEntry:
        return RepoEntry(repo=repo or self.repo, allowlisted=allowlisted)

    def config(self, *entries: RepoEntry) -> AppConfig:
        wanted = list(entries) or [self.entry()]
        return load_config(EXAMPLE).model_copy(update={"repos": wanted})


@pytest.fixture
async def world(scratch: Scratch) -> AsyncIterator[World]:
    settings = EnvSettings()
    client = await connect(settings.temporal_address, namespace=settings.temporal_namespace)
    suffix = uuid.uuid4().hex[:8]
    made = World(
        fake=FakeGitHub(),
        client=client,
        scratch=scratch,
        suffix=suffix,
        poller_queue=f"lh-poller-test-{suffix}",
        task_queue=f"lh-poller-test-tasks-{suffix}",
    )
    made.fake.seed_repo(made.repo)
    try:
        yield made
    finally:
        await end_the_tasks(made)


async def end_the_tasks(world: World) -> None:
    """Terminate the TaskWorkflows this test started. Nothing else has these ids."""
    query = f'WorkflowType = "TaskWorkflow" AND TaskQueue = "{world.task_queue}"'
    async for found in world.client.list_workflows(query):
        if found.status == WorkflowExecutionStatus.RUNNING:
            await world.client.get_workflow_handle(found.id).terminate("test is over")


@asynccontextmanager
async def polling(world: World, config: AppConfig) -> AsyncIterator[None]:
    """A worker for the poller workflow, with the real poll_once wired to the fake GitHub."""
    engine = make_engine(world.scratch.url)
    try:
        async with GitHubClient(
            FAKE_BASE_URL, PatAuth(TOKEN), transport=world.fake.transport()
        ) as github:
            activities = PollerActivities(
                github, SqlTaskStore.from_engine(engine), config, task_queue=world.task_queue
            )
            async with Worker(
                world.client,
                task_queue=world.poller_queue,
                workflows=[PollerWorkflow],
                activities=activities.activities,
                workflow_runner=workflow_runner(),
            ):
                yield
    finally:
        await engine.dispose()


async def delete_schedule(handle: ScheduleHandle) -> None:
    """Delete a schedule that the test made. One that was never made is not an error."""
    with contextlib.suppress(RPCError):
        await handle.delete()


async def run_poll(world: World) -> acts.PollResult:
    """One run of the poller workflow, as the schedule starts it."""
    return await world.client.execute_workflow(
        PollerWorkflow.run,
        id=f"lh-poller-test-run-{uuid.uuid4().hex[:8]}",
        task_queue=world.poller_queue,
        execution_timeout=timedelta(minutes=2),
    )


async def task_ids(world: World, *, expect: int) -> list[str]:
    """The ids of the TaskWorkflows of this test, once the server lists `expect` of them.

    The list comes from visibility, which trails the workflow by a moment. When `expect` is
    reached the list is read once more after a pause, so an extra workflow would show.
    """
    query = f'WorkflowType = "TaskWorkflow" AND TaskQueue = "{world.task_queue}"'
    found: list[str] = []
    async with asyncio.timeout(WAIT_S):
        while True:
            found = sorted([w.id async for w in world.client.list_workflows(query)])
            if len(found) >= expect:
                break
            await asyncio.sleep(0.2)
    await asyncio.sleep(1.0)
    return sorted([w.id async for w in world.client.list_workflows(query)])


# --- the three tests of the story -------------------------------------------------------------


async def test_starts_one_task_per_issue(world: World) -> None:
    first = world.fake.seed_issue(world.repo, "add() is wrong", labels=[LABEL])
    second = world.fake.seed_issue(world.repo, "sub() is wrong", labels=[LABEL])
    unlabeled = world.fake.seed_issue(world.repo, "just a question")

    async with polling(world, world.config()):
        first_poll = await run_poll(world)
        second_poll = await run_poll(world)

    assert first_poll == acts.PollResult(repos=1, issues=2, started=2, known=0)
    assert second_poll == acts.PollResult(repos=1, issues=2, started=0, known=2)
    assert await task_ids(world, expect=2) == [world.task_id(first), world.task_id(second)]
    with pytest.raises(RPCError) as missing:
        await world.client.get_workflow_handle(world.task_id(unlabeled)).describe()
    assert missing.value.status is RPCStatusCode.NOT_FOUND

    rows = world.scratch.query(
        "SELECT id, repo, issue_number, status, phase FROM tasks ORDER BY id"
    )
    assert rows == [
        (world.task_id(number), world.repo, number, "active", "queued")
        for number in sorted([first, second])
    ]
    described = await world.client.get_workflow_handle(world.task_id(first)).describe()
    assert described.task_queue == world.task_queue
    assert world.fake.label_events(world.repo) == [], "the poller sets no label"
    assert world.fake.count_comments(world.repo) == 0, "and posts no comment"


async def test_ignores_non_allowlisted(world: World) -> None:
    off, absent = f"acme/off-{world.suffix}", f"acme/absent-{world.suffix}"
    for repo in (off, absent):
        world.fake.seed_repo(repo)
        world.fake.seed_issue(repo, "fix slug()", labels=[LABEL])
    config = world.config(world.entry(), world.entry(off, allowlisted=False))

    async with polling(world, config):
        result = await run_poll(world)

    assert result == acts.PollResult(repos=1, issues=0, started=0, known=0)
    assert await task_ids(world, expect=0) == []
    assert world.scratch.query("SELECT id FROM tasks") == []
    asked = {request.path for request in world.fake.requests}
    assert not any(off in path or absent in path for path in asked), "never even looked"
    flags = dict(world.scratch.query("SELECT repo, allowlisted FROM repo_settings"))
    assert flags == {world.repo: True, off: False}


async def test_schedule_install_idempotent(world: World) -> None:
    schedule_id = f"lh-poller-test-{world.suffix}"
    handle = world.client.get_schedule_handle(schedule_id)
    options = {
        "interval_seconds": 3600,
        "task_queue": world.poller_queue,
        "schedule_id": schedule_id,
    }
    try:
        first = await schedule.install_poller(world.client, **options)
        created = await handle.describe()
        # The server fires an interval schedule at multiples of the interval. Pause at once so
        # that a run on the hour cannot start while the test is still looking.
        await handle.pause(note="test")
        before = await handle.describe()

        again = await schedule.install_poller(world.client, **options)

        after = await handle.describe()
        assert (first, again) == (schedule.Installed.CREATED, schedule.Installed.UNCHANGED)
        action = created.schedule.action
        assert isinstance(action, ScheduleActionStartWorkflow)
        assert action.workflow == "PollerWorkflow"
        assert action.task_queue == world.poller_queue
        assert action.execution_timeout == acts.POLLER_RUN_TIMEOUT
        assert [spec.every for spec in created.schedule.spec.intervals] == [timedelta(hours=1)]
        assert created.schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
        assert created.schedule.state.paused is False
        # The second install wrote nothing: the schedule, its pause and its update time are as
        # they were before it.
        assert after.info.last_updated_at == before.info.last_updated_at
        assert after.schedule.state == before.schedule.state
        assert schedule.fingerprint(after.schedule) == schedule.fingerprint(created.schedule)
    finally:
        await delete_schedule(handle)


# --- what only a server shows -----------------------------------------------------------------


async def test_an_install_with_a_new_interval_is_written_and_keeps_the_pause(world: World) -> None:
    schedule_id = f"lh-poller-test-{world.suffix}"
    handle = world.client.get_schedule_handle(schedule_id)
    options = {"task_queue": world.poller_queue, "schedule_id": schedule_id}
    try:
        await schedule.install_poller(world.client, interval_seconds=3600, **options)
        await handle.pause(note="maintenance")

        result = await schedule.install_poller(world.client, interval_seconds=7200, **options)
        after = await handle.describe()
        again = await schedule.install_poller(world.client, interval_seconds=7200, **options)

        assert (result, again) == (schedule.Installed.UPDATED, schedule.Installed.UNCHANGED)
        assert [spec.every for spec in after.schedule.spec.intervals] == [timedelta(hours=2)]
        assert after.schedule.state.paused is True
        assert after.schedule.state.note == "maintenance"
        assert after.info.last_updated_at is not None
    finally:
        await delete_schedule(handle)


async def test_pause_resume_and_trigger_manage_the_schedule(world: World) -> None:
    schedule_id = f"lh-poller-test-{world.suffix}"
    handle = world.client.get_schedule_handle(schedule_id)
    first = world.fake.seed_issue(world.repo, "add() is wrong", labels=[LABEL])
    try:
        async with polling(world, world.config()):
            await schedule.install_poller(
                world.client,
                interval_seconds=3600,
                task_queue=world.poller_queue,
                schedule_id=schedule_id,
            )

            await schedule.pause_poller(world.client, note="deploying", schedule_id=schedule_id)
            assert (await handle.describe()).schedule.state.paused is True

            # A trigger runs the poller whether or not the schedule is paused.
            await schedule.trigger_poller(world.client, schedule_id=schedule_id)
            assert await task_ids(world, expect=1) == [world.task_id(first)]

            await schedule.resume_poller(world.client, schedule_id=schedule_id)
            assert (await handle.describe()).schedule.state.paused is False
            await handle.pause(note="test")  # so that no tick fires while the test ends
    finally:
        await delete_schedule(handle)


async def test_a_schedule_that_does_not_exist_is_reported_as_missing(world: World) -> None:
    missing = f"lh-poller-test-{world.suffix}"

    for action in (schedule.pause_poller, schedule.resume_poller, schedule.trigger_poller):
        with pytest.raises(schedule.ScheduleMissingError, match="longhaul poller install"):
            await action(world.client, schedule_id=missing)


# --- the tasks table --------------------------------------------------------------------------


def spec_for(world: World, number: int, title: str = "add() is wrong") -> TaskSpec:
    return TaskSpec(
        task_id=world.task_id(number),
        repo=world.repo,
        issue_number=number,
        title=title,
        body="",
        settings=RepoSettings(repo=world.repo, test_command="pytest -x"),
    )


async def test_a_pickup_row_is_written_once_and_a_later_status_is_kept(world: World) -> None:
    engine = make_engine(world.scratch.url)
    try:
        store = SqlTaskStore.from_engine(engine)
        await store.record_pickup(spec_for(world, 1))
        world.scratch.execute("UPDATE tasks SET status = 'done', phase = 'done'")

        await store.record_pickup(spec_for(world, 1, title="the title changed"))

        rows = world.scratch.query("SELECT id, title, status, phase, settings FROM tasks")
        assert len(rows) == 1
        task_id, title, status, phase, settings = rows[0]
        assert (task_id, status, phase) == (world.task_id(1), "done", "done")
        assert title == "the title changed", "the title follows the issue"
        assert settings["test_command"] == "pytest -x"
        started = world.scratch.query("SELECT started_at IS NOT NULL FROM tasks")
        assert started == [(True,)]
    finally:
        await engine.dispose()


async def test_known_answers_with_the_ids_that_have_a_row(world: World) -> None:
    engine = make_engine(world.scratch.url)
    try:
        store = SqlTaskStore.from_engine(engine)
        await store.record_pickup(spec_for(world, 1))
        await store.record_pickup(spec_for(world, 3))

        ids = [world.task_id(n) for n in (1, 2, 3)]

        assert await store.known(ids) == {world.task_id(1), world.task_id(3)}
        assert await store.known([]) == set()
    finally:
        await engine.dispose()
