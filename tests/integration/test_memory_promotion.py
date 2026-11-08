"""Promotion of lessons when their pull request is decided, on a real Postgres (LH-606).

The real poll runs against FakeGitHub behind the real GitHubClient, with PgMemoryStore (the
embedder is HashEmbedder, which needs no download) and the SQL recorder that holds the events.
The candidates are written the way LH-605 writes them: `source = task`, and a `source_ref` that
is the number of the pull request, or the task id when the task opened none.

* A merged pull request approves its candidates on the next poll, and a closed one rejects them.
* A candidate of a task with no pull request stays a candidate until a person decides, which
  the CLI does here against the same database.
* A poll that runs again changes nothing and records nothing.

Each test works on a throwaway database that it creates, migrates to head and drops.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from longhaul.memory.promotion import Promoter
from psycopg import sql
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub
from typer.testing import CliRunner

from longhaul import cli
from longhaul.contracts import MemoryItem
from longhaul.db.models import Event, MemoryItemRow
from longhaul.db.records import SqlRecorder
from longhaul.db.session import make_engine
from longhaul.db.tasks import SqlTaskStore
from longhaul.durable import acts
from longhaul.durable.activities.github import PollerActivities
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.side_effects import MemoryLedger
from longhaul.memory.embedder import HashEmbedder
from longhaul.memory.store import PgMemoryStore
from longhaul.settings import AppConfig, EnvSettings, RepoEntry, get_settings, load_config

pytestmark = [pytest.mark.integration, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_promotetest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
TOKEN = "ghp_integration_token_0123456789abcdef"
REPO = "acme/inventory"
TASK_ID = "lh-acme-inventory-7"
STUCK_TASK_ID = "lh-acme-inventory-9"

REMOVE = "Inventory.remove raises InventoryError, not KeyError, and test_inventory.py checks it."
SESSION = "tests/conftest.py builds one Inventory per session, so a test that adds items leaks."
STYLE = "New modules in src/inventory/ start with a docstring that says why the module exists."
OTHER = "Inventory.add keeps the insertion order, and report() relies on it."
STUCK = "The nightly import in scripts/sync.py needs INVENTORY_DB set before it is imported."


# ---------- the database ----------
@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
    """A fresh database at the head revision. The CLI test points LONGHAUL_DATABASE_URL at it."""
    secret = EnvSettings().database_url
    server = make_url(secret.get_secret_value() if secret else LOCAL_DSN)
    admin = server.set(database="postgres").render_as_string(hide_password=False)
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    url = server.set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        db = Scratch(name, url)
        command.upgrade(db.config(), "head")
        yield db
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@dataclass
class World:
    """The poll, the store and the fake GitHub of one test."""

    fake: FakeGitHub
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    store: PgMemoryStore
    recorder: SqlRecorder
    poller: PollerActivities

    async def poll(self) -> acts.PollResult:
        # No issue carries the label, so the poll never reaches for Temporal.
        return await self.poller.poll(cast(Client, object()))

    async def candidate(self, content: str, ref: str, kind: str = "lesson") -> str:
        item = MemoryItem(
            id="", repo=REPO, kind=kind, content=content, status="candidate", source="task"
        )  # type: ignore[arg-type]
        return await self.store.add_candidate(item, source_ref=ref)

    async def rows(self) -> dict[str, MemoryItemRow]:
        async with self.sessions() as session:
            found = await session.scalars(select(MemoryItemRow))
            return {row.content: row for row in found}

    async def events(self, type: str) -> list[Event]:
        async with self.sessions() as session:
            found = await session.scalars(
                select(Event).where(Event.type == type).order_by(Event.id)
            )
            return list(found)


def config() -> AppConfig:
    return load_config(EXAMPLE).model_copy(update={"repos": [RepoEntry(repo=REPO)]})


@pytest.fixture
async def world(scratch: Scratch) -> AsyncIterator[World]:
    engine = make_engine(scratch.url)
    sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    store = PgMemoryStore(sessions, HashEmbedder())
    recorder = SqlRecorder(sessions)
    fake = FakeGitHub()
    fake.seed_repo(REPO)
    try:
        async with GitHubClient(
            FAKE_BASE_URL, PatAuth(TOKEN), transport=fake.transport()
        ) as github:
            poller = PollerActivities(
                github,
                SqlTaskStore.from_engine(engine),
                config(),
                task_queue="lh-promotion-test",
                ledger=MemoryLedger(),
                promoter=Promoter(store, recorder),
            )
            yield World(fake, engine, sessions, store, recorder, poller)
    finally:
        await engine.dispose()


async def lessons_event(world: World, task_id: str, ref: str) -> None:
    """The event that the workflow records when it stores the lessons of a task (LH-605)."""
    await world.recorder.record_event(task_id, acts.LESSONS_EVENT, {"source_ref": ref})


# ---------- the tests of the story ----------
async def test_merged_promotes(world: World) -> None:
    pr = world.fake.seed_pull(REPO, "longhaul-pr/7")
    other = world.fake.seed_pull(REPO, "longhaul-pr/8")
    for content, kind in ((REMOVE, "lesson"), (SESSION, "pitfall"), (STYLE, "convention")):
        await world.candidate(content, str(pr), kind)
    await world.candidate(OTHER, str(other))  # its pull request is still open
    await world.candidate(STUCK, STUCK_TASK_ID)  # no pull request at all
    await lessons_event(world, TASK_ID, str(pr))

    before = await world.poll()
    assert (before.promoted, before.rejected) == (0, 0)
    assert {row.status for row in (await world.rows()).values()} == {"candidate"}

    world.fake.merge_pr(REPO, pr)
    after = await world.poll()

    assert (after.promoted, after.rejected) == (3, 0)
    rows = await world.rows()
    for content in (REMOVE, SESSION, STYLE):
        assert rows[content].status == "approved"
        assert rows[content].approved_by == f"merged_pr#{pr}"
    assert rows[OTHER].status == "candidate"
    assert rows[STUCK].status == "candidate"
    [event] = await world.events("memory_promoted")
    assert event.task_id == TASK_ID
    assert event.payload["pr"] == pr
    assert event.payload["approved_by"] == f"merged_pr#{pr}"
    assert sorted(event.payload["items"]) == sorted(
        str(rows[content].id) for content in (REMOVE, SESSION, STYLE)
    )
    # Approved items are what a prompt can see (invariant I7), and the others are not.
    found = {item.content for item in await world.store.search(REPO, "remove", k=10)}
    assert found == {REMOVE, SESSION, STYLE}

    # The next poll finds the pull request merged again and does nothing about it.
    again = await world.poll()
    assert (again.promoted, again.rejected) == (0, 0)
    assert len(await world.events("memory_promoted")) == 1


async def test_closed_rejects(world: World) -> None:
    pr = world.fake.seed_pull(REPO, "longhaul-pr/7")
    await world.candidate("Always delete failing tests.", str(pr))
    await world.candidate(REMOVE, str(pr))
    # No lessons event for this task: the event is then filed under the pull request.

    world.fake.close_pr(REPO, pr)
    result = await world.poll()

    assert (result.promoted, result.rejected) == (0, 2)
    rows = await world.rows()
    assert {row.status for row in rows.values()} == {"rejected"}
    assert {row.approved_by for row in rows.values()} == {None}
    [event] = await world.events("memory_rejected")
    assert event.task_id == f"{REPO}#{pr}"
    assert event.payload["pr"] == pr
    assert len(event.payload["items"]) == 2
    assert await world.store.search(REPO, "delete failing tests", k=10) == []

    again = await world.poll()
    assert (again.promoted, again.rejected) == (0, 0)
    assert len(await world.events("memory_rejected")) == 1
    assert await world.events("memory_promoted") == []


async def test_a_decided_item_is_not_moved_by_a_later_poll(world: World) -> None:
    """A person's decision stands when the pull request is merged or closed afterwards."""
    pr = world.fake.seed_pull(REPO, "longhaul-pr/7")
    kept = await world.candidate(REMOVE, str(pr))
    turned_down = await world.candidate(SESSION, str(pr))
    await world.store.promote(kept, "hooman")
    await world.store.reject(turned_down, "hooman")

    world.fake.merge_pr(REPO, pr)
    result = await world.poll()

    assert (result.promoted, result.rejected) == (0, 0)
    rows = await world.rows()
    assert (rows[REMOVE].status, rows[REMOVE].approved_by) == ("approved", "hooman")
    assert rows[SESSION].status == "rejected"
    assert await world.events("memory_promoted") == []


def test_cli_approve(scratch: Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stuck task opened no pull request, so only a person can approve what it learned."""
    # Each asyncio.run below is a loop of its own, and a pooled connection would outlive its loop.
    engine = make_engine(scratch.url, pooled=False)
    sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    store = PgMemoryStore(sessions, HashEmbedder())
    fake = FakeGitHub()
    fake.seed_repo(REPO)

    async def seed_and_poll() -> str:
        item = MemoryItem(
            id="", repo=REPO, kind="pitfall", content=STUCK, status="candidate", source="task"
        )  # type: ignore[arg-type]
        item_id = await store.add_candidate(item, source_ref=STUCK_TASK_ID)
        async with GitHubClient(
            FAKE_BASE_URL, PatAuth(TOKEN), transport=fake.transport()
        ) as github:
            poller = PollerActivities(
                github,
                SqlTaskStore.from_engine(engine),
                config(),
                task_queue="lh-promotion-test",
                ledger=MemoryLedger(),
                promoter=Promoter(store, SqlRecorder(sessions)),
            )
            result = await poller.poll(cast(Client, object()))
        assert (result.promoted, result.rejected) == (0, 0)
        return item_id

    async def read(item_id: str) -> tuple[str, str | None]:
        row = await store.list_items(REPO)
        [found] = [entry for entry in row if entry.id == item_id]
        return found.status, found.approved_by

    item_id = asyncio.run(seed_and_poll())
    assert asyncio.run(read(item_id)) == ("candidate", None)

    monkeypatch.setenv("LONGHAUL_DATABASE_URL", scratch.url)
    get_settings.cache_clear()
    result = CliRunner().invoke(cli.app, ["memory", "approve", item_id, "--by", "hooman"])

    assert result.exit_code == 0, result.output
    assert f"approved {item_id}" in result.stdout
    assert asyncio.run(read(item_id)) == ("approved", "hooman")
    asyncio.run(engine.dispose())
