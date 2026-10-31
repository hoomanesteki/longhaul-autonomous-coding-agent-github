"""Idempotent GitHub writes on Postgres, through the activities and a Temporal worker (LH-304).

The first three tests are the ones the story names: failure mode F10 (a write that landed but
answered 502), failure mode F11 (the ledger lost, the comment still on GitHub), and a pull
request that is already open for the head branch. The rest check what only a real database
and a real worker can show: the lock between overlapping attempts, a cancelled attempt that
lets go of its key, and Temporal's own retry of the error the activity raises.

GitHub is FakeGitHub on its ASGI transport. Each test gets its own throwaway database that it
creates, migrates and drops, like the other migration-based integration tests do.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, LiteralString

import httpx2
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from pydantic import BaseModel
from sqlalchemy.engine import make_url
from temporalio import workflow
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.db.session import make_engine
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.side_effects import (
    CommentInput,
    Effect,
    LabelInput,
    PullRequestInput,
    SideEffectActivities,
    SqlLedger,
    effect_key,
    marker,
    payload_hash,
)
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_migtest_"
# If env.py or the code under test ever ignored the scratch URL it would fall back to this and
# fail to connect, instead of reaching a real database.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"

REPO = "acme/calc"
ISSUE = 1
TASK = "lh-acme-calc-1"
TOKEN = "ghp_integration_token_0123456789abcdef"
HEAD = "longhaul-pr/1"
LABEL = "longhaul:working"
COMMENT_KEY = effect_key(TASK, "picked_up", 1)
PR_KEY = effect_key(TASK, "pr", 1)
QUEUE = "side-effects-test"


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


# --- the world: FakeGitHub, the database and the activities -----------------------------------


class SlowTransport(httpx2.AsyncBaseTransport):
    """Delays every request, so that callers who overlap in time really overlap."""

    def __init__(self, inner: httpx2.AsyncBaseTransport, delay_s: float) -> None:
        self._inner = inner
        self._delay_s = delay_s
        self._in_flight = 0
        self.peak = 0

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self._in_flight += 1
        self.peak = max(self.peak, self._in_flight)
        try:
            await asyncio.sleep(self._delay_s)
            return await self._inner.handle_async_request(request)
        finally:
            self._in_flight -= 1


@dataclass
class World:
    fake: FakeGitHub
    scratch: Scratch
    activities: SideEffectActivities

    def rows(self) -> list[tuple[Any, ...]]:
        return self.scratch.query(
            "SELECT key, task_id, kind, external_id FROM side_effects ORDER BY key"
        )

    def lose_the_database(self) -> None:
        """What a restore from an old backup or a dropped volume does to the ledger."""
        self.scratch.execute("TRUNCATE side_effects")

    async def run(self, fn: Callable[..., Any], inp: BaseModel) -> str:
        result: str = await ActivityEnvironment().run(fn, inp)
        return result


def github_on(transport: httpx2.AsyncBaseTransport) -> GitHubClient:
    return GitHubClient(FAKE_BASE_URL, PatAuth(TOKEN), transport=transport)


@pytest.fixture
async def world(scratch: Scratch) -> AsyncIterator[World]:
    fake = FakeGitHub()
    fake.seed_repo(REPO)
    fake.seed_issue(REPO, "add() is wrong")
    engine = make_engine(scratch.url)
    try:
        async with github_on(fake.transport()) as github:
            ledger = SqlLedger.from_engine(engine)
            yield World(fake, scratch, SideEffectActivities(github, ledger))
    finally:
        await engine.dispose()


def comment(body: str = "Picked up", key: str = COMMENT_KEY) -> CommentInput:
    return CommentInput(task_id=TASK, repo=REPO, number=ISSUE, body=body, idempotency_key=key)


def label(key: str, name: str = LABEL) -> LabelInput:
    return LabelInput(task_id=TASK, repo=REPO, number=ISSUE, label=name, idempotency_key=key)


def pull(**changes: Any) -> PullRequestInput:
    fields: dict[str, Any] = {
        "task_id": TASK,
        "repo": REPO,
        "head": HEAD,
        "base": "main",
        "title": "fix(calc): add returns the sum",
        "body": "Closes #1",
        "draft": True,
        "idempotency_key": PR_KEY,
    }
    return PullRequestInput(**{**fields, **changes})


# --- the three cases of the story -------------------------------------------------------------


async def test_retry_after_5xx_no_duplicate(world: World) -> None:
    # F10. The comment is stored and GitHub answers 502, so the caller cannot know it landed.
    world.fake.after_write_fail(r"/issues/1/comments$")

    with pytest.raises(ApplicationError) as failed:
        await world.run(world.activities.post_comment, comment())

    assert failed.value.non_retryable is False, "Temporal must retry this one"
    assert world.fake.count_comments(REPO, ISSUE) == 1, "the write landed despite the 502"
    assert world.rows() == [], "nothing was recorded, so the retry has to find the comment"

    comment_id = await world.run(world.activities.post_comment, comment())  # the retry

    assert world.fake.count_comments(REPO, ISSUE) == 1, "exactly one comment, not two"
    assert world.rows() == [(COMMENT_KEY, TASK, "comment", comment_id)]
    assert [(r.method, r.status) for r in world.fake.requests if r.method == "POST"] == [
        ("POST", 502)
    ], "the retry searched, found the comment and did not post again"


async def test_comment_idempotent_after_db_loss(world: World) -> None:
    # F11. The ledger row is gone, GitHub still has the comment, and the marker leads to it.
    first = await world.run(world.activities.post_comment, comment())
    assert world.rows() == [(COMMENT_KEY, TASK, "comment", first)]

    world.lose_the_database()
    assert world.rows() == []

    again = await world.run(world.activities.post_comment, comment())

    assert again == first
    assert world.fake.count_comments(REPO, ISSUE) == 1, "no new comment"
    assert world.fake.count_comments(REPO, ISSUE, containing=marker(COMMENT_KEY)) == 1
    assert world.rows() == [(COMMENT_KEY, TASK, "comment", first)], "the row is back"


async def test_pr_idempotent_by_head(world: World) -> None:
    number = world.fake.seed_pull(REPO, HEAD, title="opened by an earlier attempt")

    url = await world.run(world.activities.create_pr, pull())

    assert url == f"https://github.com/{REPO}/pull/{number}"
    assert world.fake.count_pulls(REPO) == 1, "no second pull request for the same head"
    assert world.rows() == [(PR_KEY, TASK, "pr", url)]

    world.lose_the_database()
    assert await world.run(world.activities.create_pr, pull()) == url
    assert world.fake.count_pulls(REPO) == 1


# --- the rest of the contract on Postgres -----------------------------------------------------


async def test_every_kind_of_write_leaves_one_row_and_a_second_call_adds_nothing(
    world: World,
) -> None:
    add, remove = effect_key(TASK, "add", 1), effect_key(TASK, "remove", 1)
    calls = [
        (world.activities.post_comment, comment()),
        (world.activities.add_label, label(add)),
        (world.activities.create_pr, pull()),
        (world.activities.remove_label, label(remove)),
    ]

    first = [await world.run(fn, inp) for fn, inp in calls]
    rows, requests = world.rows(), len(world.fake.requests)
    second = [await world.run(fn, inp) for fn, inp in calls]

    assert second == first
    assert world.rows() == rows and len(world.fake.requests) == requests
    assert sorted((kind, key) for key, _, kind, _ in rows) == sorted(
        [
            ("comment", COMMENT_KEY),
            ("label_add", add),
            ("label_remove", remove),
            ("pr", PR_KEY),
        ]
    )
    assert world.fake.label_names(REPO, ISSUE) == []
    assert world.fake.count_pulls(REPO) == world.fake.count_comments(REPO, ISSUE) == 1


async def test_the_row_holds_the_hash_of_the_payload(world: World) -> None:
    await world.run(world.activities.post_comment, comment("Picked up"))

    [(stored,)] = world.scratch.query("SELECT payload_hash FROM side_effects")

    expected = payload_hash("comment", {"repo": REPO, "number": ISSUE, "body": "Picked up"})
    assert stored == expected


async def test_a_key_that_names_another_effect_is_refused_and_writes_nothing(
    world: World,
) -> None:
    await world.run(world.activities.post_comment, comment("Picked up"))

    with pytest.raises(ApplicationError) as failed:
        await world.run(world.activities.post_comment, comment("Plan ready"))

    assert (failed.value.type, failed.value.non_retryable) == ("KeyReuseError", True)
    assert world.fake.count_comments(REPO, ISSUE) == 1
    assert len(world.rows()) == 1


async def test_the_sql_ledger_keeps_the_first_row_of_a_key(scratch: Scratch) -> None:
    # Two writers can only meet here if the lock failed. The row of the first one stays.
    first = Effect(COMMENT_KEY, TASK, "comment", "1001", "a" * 64)
    second = Effect(COMMENT_KEY, TASK, "comment", "1002", "a" * 64)
    engine = make_engine(scratch.url)
    try:
        ledger = SqlLedger.from_engine(engine)
        async with ledger.locked(COMMENT_KEY) as entry:
            assert await entry.get(COMMENT_KEY) is None
            assert await entry.record(first) == first
            assert await entry.record(second) == first
        async with ledger.locked(COMMENT_KEY) as entry:  # a new transaction sees the commit
            assert await entry.get(COMMENT_KEY) == first
    finally:
        await engine.dispose()

    assert scratch.query("SELECT external_id FROM side_effects") == [("1001",)]


async def test_a_block_that_fails_leaves_no_row(scratch: Scratch) -> None:
    engine = make_engine(scratch.url)
    try:
        ledger = SqlLedger.from_engine(engine)
        with pytest.raises(RuntimeError):
            async with ledger.locked(COMMENT_KEY) as entry:
                await entry.record(Effect(COMMENT_KEY, TASK, "comment", "1001", "a" * 64))
                raise RuntimeError("something after the row failed")
        async with ledger.locked(COMMENT_KEY) as entry:
            assert await entry.get(COMMENT_KEY) is None
    finally:
        await engine.dispose()

    assert scratch.query("SELECT key FROM side_effects") == []


async def test_overlapping_attempts_on_two_connections_post_once(
    world: World, scratch: Scratch
) -> None:
    # A timed-out attempt may still be running when the next one starts. They share the
    # database but not a process, so the lock has to be Postgres's.
    slow = SlowTransport(world.fake.transport(), delay_s=0.05)
    engine = make_engine(scratch.url)
    try:
        async with github_on(slow) as one, github_on(slow) as two:
            first = SideEffectActivities(one, SqlLedger.from_engine(engine))
            second = SideEffectActivities(two, SqlLedger.from_engine(engine))
            ids = await asyncio.gather(
                world.run(first.post_comment, comment()),
                world.run(second.post_comment, comment()),
            )
    finally:
        await engine.dispose()

    assert ids[0] == ids[1]
    assert world.fake.count_comments(REPO, ISSUE) == 1
    assert slow.peak == 1, "the second attempt waited for the first"
    assert len(world.rows()) == 1


async def test_different_keys_of_one_task_do_not_wait_for_each_other(
    world: World, scratch: Scratch
) -> None:
    slow = SlowTransport(world.fake.transport(), delay_s=0.05)
    engine = make_engine(scratch.url)
    try:
        async with github_on(slow) as one, github_on(slow) as two:
            first = SideEffectActivities(one, SqlLedger.from_engine(engine))
            second = SideEffectActivities(two, SqlLedger.from_engine(engine))
            await asyncio.gather(
                world.run(first.post_comment, comment("one", effect_key(TASK, "one", 1))),
                world.run(second.post_comment, comment("two", effect_key(TASK, "two", 1))),
            )
    finally:
        await engine.dispose()

    assert world.fake.count_comments(REPO, ISSUE) == 2
    assert slow.peak == 2
    assert len(world.rows()) == 2


async def test_a_cancelled_attempt_lets_go_of_its_key(world: World, scratch: Scratch) -> None:
    # A worker that shuts down cancels the activity in the middle of a request. The next
    # attempt of the same key must not wait for a lock that nobody holds any more.
    stalled = SlowTransport(world.fake.transport(), delay_s=60)
    engine = make_engine(scratch.url)
    try:
        async with github_on(stalled) as client:
            doomed = SideEffectActivities(client, SqlLedger.from_engine(engine))
            attempt = asyncio.create_task(world.run(doomed.post_comment, comment()))
            while stalled.peak == 0:
                await asyncio.sleep(0.01)  # until it holds the key and waits on GitHub
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
    finally:
        await engine.dispose()

    retried = await asyncio.wait_for(world.run(world.activities.post_comment, comment()), 20)

    assert world.fake.count_comments(REPO, ISSUE) == 1
    assert world.rows() == [(COMMENT_KEY, TASK, "comment", retried)]


async def test_a_lost_database_connection_is_retried_not_swallowed(
    world: World, scratch: Scratch
) -> None:
    broken = make_engine("postgresql://nobody:nopass@127.0.0.1:1/never")
    try:
        async with github_on(world.fake.transport()) as client:
            activities = SideEffectActivities(client, SqlLedger.from_engine(broken))
            with pytest.raises(OSError):  # ConnectionRefusedError, which Temporal retries
                await world.run(activities.post_comment, comment())
    finally:
        await broken.dispose()

    assert world.fake.requests == [], "no write without a ledger to record it in"


# --- under a Temporal worker ------------------------------------------------------------------

RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=5)


class Steps(BaseModel):
    comment: CommentInput
    add: LabelInput
    remove: LabelInput
    pr: PullRequestInput


@workflow.defn(sandboxed=False)
class PostCommentProbe:
    """Calls post_comment by name with the retry policy of section 3.7, as TaskWorkflow will."""

    @workflow.run
    async def run(self, inp: CommentInput) -> str:
        return await workflow.execute_activity(
            "post_comment",
            inp,
            result_type=str,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=RETRY,
        )


@workflow.defn(sandboxed=False)
class AllFourProbe:
    @workflow.run
    async def run(self, steps: Steps) -> list[str]:
        async def call(name: str, inp: BaseModel) -> str:
            return await workflow.execute_activity(
                name,
                inp,
                result_type=str,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=RETRY,
            )

        return [
            await call("post_comment", steps.comment),
            await call("add_label", steps.add),
            await call("create_pr", steps.pr),
            await call("remove_label", steps.remove),
        ]


async def test_temporal_runs_the_four_activities_by_name(world: World) -> None:
    add, remove = effect_key(TASK, "add", 1), effect_key(TASK, "remove", 1)
    steps = Steps(comment=comment(), add=label(add), remove=label(remove), pr=pull())

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env,
        Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[AllFourProbe],
            activities=world.activities.activities,
        ),
    ):
        result = await env.client.execute_workflow(
            AllFourProbe.run, steps, id=f"probe-{uuid.uuid4()}", task_queue=QUEUE
        )

    assert result[1:3] == [LABEL, f"https://github.com/{REPO}/pull/2"]
    assert {kind for _, _, kind, _ in world.rows()} == {
        "comment",
        "label_add",
        "label_remove",
        "pr",
    }


async def test_temporal_retries_a_502_and_the_comment_lands_once(world: World) -> None:
    world.fake.after_write_fail(r"/issues/1/comments$")

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env,
        Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[PostCommentProbe],
            activities=world.activities.activities,
        ),
    ):
        comment_id = await env.client.execute_workflow(
            PostCommentProbe.run, comment(), id=f"probe-{uuid.uuid4()}", task_queue=QUEUE
        )

    assert world.fake.count_comments(REPO, ISSUE) == 1
    assert world.rows() == [(COMMENT_KEY, TASK, "comment", comment_id)]
    # Attempt 1: search, post (landed, 502). Attempt 2: search, found.
    assert [r.method for r in world.fake.requests] == ["GET", "POST", "GET"]


async def test_temporal_does_not_retry_an_error_that_retrying_cannot_fix(world: World) -> None:
    missing = CommentInput(
        task_id=TASK, repo=REPO, number=99, body="hi", idempotency_key=COMMENT_KEY
    )

    async with (
        await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter
        ) as env,
        Worker(
            env.client,
            task_queue=QUEUE,
            workflows=[PostCommentProbe],
            activities=world.activities.activities,
        ),
    ):
        with pytest.raises(WorkflowFailureError) as failed:
            await env.client.execute_workflow(
                PostCommentProbe.run, missing, id=f"probe-{uuid.uuid4()}", task_queue=QUEUE
            )

    activity_error = failed.value.cause
    assert isinstance(activity_error, ActivityError)
    cause = activity_error.cause
    assert isinstance(cause, ApplicationError)
    assert (cause.type, cause.non_retryable) == ("GitHubFatal", True)
    assert len(world.fake.requests) == 1, "one attempt, no retry"
    assert world.rows() == []
