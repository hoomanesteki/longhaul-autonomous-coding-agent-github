"""PgMemoryStore on a real Postgres with pgvector (LH-603).

Each test works on a throwaway database that it creates, migrates to head and drops, so the
application database is never touched. The embedder is HashEmbedder: it needs no download and
ranks by shared words, which is enough to say which note is nearest.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pgvector import Vector
from psycopg import sql
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from longhaul.contracts import MemoryItem
from longhaul.db.models import MemoryItemRow
from longhaul.db.session import make_engine, to_async_url
from longhaul.memory.embedder import HashEmbedder
from longhaul.memory.store import (
    DEFAULT_BRANCH_APPROVER,
    MemoryItemNotFoundError,
    PgMemoryStore,
)
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_memtest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REPO = "acme/inventory"
OTHER_REPO = "acme/calc"
SHA = "a" * 40

REMOVE_LESSON = "Raise InventoryError, not ValueError, when an item is missing from the inventory."
FORMAT_LESSON = "Format the code with ruff and keep lines under 100 characters."
LAYOUT_LESSON = "Name test files test_<module>.py and keep one test file per module."
QUERY = "Which error does remove raise for a missing item in the inventory?"


class Scratch:
    def __init__(self, name: str, url: str) -> None:
        self.name, self.url = name, url

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def query(self, statement: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement, params).fetchall()

    def execute(self, statement: LiteralString) -> None:
        with psycopg.connect(self.url) as conn:
            conn.execute(statement)


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
    secret = EnvSettings().database_url
    server = make_url(secret.get_secret_value() if secret else LOCAL_DSN)
    admin = server.set(database="postgres").render_as_string(hide_password=False)
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        url = server.set(database=name).render_as_string(hide_password=False)
        scratch_db = Scratch(name, url)
        command.upgrade(scratch_db.config(), "head")
        yield scratch_db
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def sessions(scratch: Scratch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(scratch.url)
    try:
        yield async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def store(sessions: async_sessionmaker[AsyncSession]) -> PgMemoryStore:
    return PgMemoryStore(sessions, HashEmbedder())


def note(content: str, **fields: Any) -> MemoryItem:
    """A candidate lesson of REPO unless the fields say otherwise. The store makes the id."""
    values: dict[str, Any] = {
        "id": "",
        "repo": REPO,
        "kind": "lesson",
        "status": "candidate",
        "source": "task",
    }
    values.update(fields)
    return MemoryItem(content=content, **values)


async def row_of(sessions: async_sessionmaker[AsyncSession], item_id: str) -> MemoryItemRow:
    async with sessions() as session:
        return (
            await session.scalars(
                select(MemoryItemRow).where(MemoryItemRow.id == uuid.UUID(item_id))
            )
        ).one()


async def count(sessions: async_sessionmaker[AsyncSession]) -> int:
    async with sessions() as session:
        return (await session.scalar(select(func.count()).select_from(MemoryItemRow))) or 0


# ---------- dedupe ----------
async def test_dedupe(store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]) -> None:
    first = await store.add_candidate(note(REMOVE_LESSON))
    # The same note typed again: other case, other spacing, a trailing newline.
    shouted = (
        "  RAISE inventoryerror,\tnot ValueError,\nwhen an item is missing from the inventory.\n"
    )
    second = await store.add_candidate(note(shouted))

    assert second == first
    assert await count(sessions) == 1
    # The first wording stays as it was written.
    assert (await row_of(sessions, first)).content == REMOVE_LESSON

    # Another kind, another repo or other words make another item.
    other_kind = await store.add_candidate(note(REMOVE_LESSON, kind="pitfall"))
    other_repo = await store.add_candidate(note(REMOVE_LESSON, repo=OTHER_REPO))
    other_words = await store.add_candidate(note(FORMAT_LESSON))
    assert len({first, other_kind, other_repo, other_words}) == 4
    assert await count(sessions) == 4


async def test_adds_that_race_make_one_item(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    ids = await asyncio.gather(*(store.add_candidate(note(REMOVE_LESSON)) for _ in range(8)))

    assert len(set(ids)) == 1
    assert await count(sessions) == 1


async def test_a_duplicate_does_not_change_the_first_item(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    # A rejected note stays rejected when a task proposes it again, and an approved one stays
    # approved with its approver.
    rejected = await store.add_candidate(note(FORMAT_LESSON))
    await store.reject(rejected, "alice")
    approved = await store.add_candidate(note(LAYOUT_LESSON))
    await store.promote(approved, "bob")

    assert await store.add_candidate(note(FORMAT_LESSON)) == rejected
    assert await store.add_candidate(note(LAYOUT_LESSON)) == approved

    assert (await row_of(sessions, rejected)).status == "rejected"
    row = await row_of(sessions, approved)
    assert (row.status, row.approved_by) == ("approved", "bob")


async def test_a_blank_note_is_refused(store: PgMemoryStore) -> None:
    with pytest.raises(ValueError, match="empty"):
        await store.add_candidate(note(" \n\t "))


# ---------- what a task may write ----------
async def test_a_task_can_only_add_candidates(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    with pytest.raises(ValueError, match="candidate"):
        await store.add_candidate(note(REMOVE_LESSON, status="approved"))
    assert await count(sessions) == 0

    item_id = await store.add_candidate(note(REMOVE_LESSON), source_ref="42")
    row = await row_of(sessions, item_id)
    assert (row.status, row.source, row.source_ref) == ("candidate", "task", "42")
    assert (row.approved_by, row.uses, row.last_used_at) == (None, 0, None)
    assert len(row.content_hash or "") == 64


async def test_verified_items_are_approved_by_the_default_branch(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    command_item = note("pytest -q", kind="command", status="approved", source="default_branch")
    item_id = await store.add_verified(command_item, source_ref=SHA)

    row = await row_of(sessions, item_id)
    assert (row.status, row.kind, row.source) == ("approved", "command", "default_branch")
    assert (row.source_ref, row.approved_by) == (SHA, DEFAULT_BRANCH_APPROVER)
    # Adding it again, from another task on the same repo, finds the item and keeps it.
    assert await store.add_verified(command_item, source_ref="b" * 40) == item_id
    assert (await row_of(sessions, item_id)).source_ref == SHA
    assert await count(sessions) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"status": "candidate"},
        {"source": "task"},
        {"source": "human"},
        {"kind": "lesson"},
        {"kind": "pitfall"},
    ],
)
async def test_only_the_default_branch_can_verify_a_map_or_a_command(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession], fields: dict[str, str]
) -> None:
    values = {"kind": "command", "status": "approved", "source": "default_branch", **fields}
    with pytest.raises(ValueError, match="default branch"):
        await store.add_verified(note("pytest -q", **values), source_ref=SHA)
    assert await count(sessions) == 0


# ---------- search ----------
async def test_search_returns_approved_items_nearest_first(store: PgMemoryStore) -> None:
    near = await store.add_candidate(note(REMOVE_LESSON))
    far = await store.add_candidate(note(LAYOUT_LESSON))
    mid = await store.add_candidate(note("Check that remove raises the error with pytest.raises."))
    for item_id in (near, far, mid):
        await store.promote(item_id, "alice")

    found = await store.search(REPO, QUERY, k=3)

    assert [item.id for item in found] == [near, mid, far]
    assert found[0] == MemoryItem(
        id=near, repo=REPO, kind="lesson", content=REMOVE_LESSON, status="approved", source="task"
    )
    assert [item.id for item in await store.search(REPO, QUERY, k=1)] == [near]


async def test_search_with_approved_only_leaves_out_candidates_and_rejects(
    store: PgMemoryStore,
) -> None:
    approved = await store.add_candidate(note(REMOVE_LESSON))
    await store.promote(approved, "alice")
    candidate = await store.add_candidate(note("Remove raises InventoryError for a missing item."))
    rejected = await store.add_candidate(note("Remove returns None for a missing item."))
    await store.reject(rejected, "alice")

    assert [i.id for i in await store.search(REPO, QUERY, k=5)] == [approved]
    assert [i.id for i in await store.search(REPO, QUERY, k=5, approved_only=True)] == [approved]

    everything = await store.search(REPO, QUERY, k=5, approved_only=False)
    assert {i.id for i in everything} == {approved, candidate, rejected}
    assert {i.status for i in everything} == {"approved", "candidate", "rejected"}


async def test_search_stays_inside_the_repo(store: PgMemoryStore) -> None:
    mine = await store.add_candidate(note(REMOVE_LESSON))
    theirs = await store.add_candidate(note(REMOVE_LESSON, repo=OTHER_REPO))
    await store.promote(mine, "alice")
    await store.promote(theirs, "alice")

    assert [i.id for i in await store.search(REPO, QUERY, k=5)] == [mine]
    assert [i.id for i in await store.search("acme/none", QUERY, k=5)] == []


async def test_search_counts_the_uses_of_what_it_returns(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    used = await store.add_candidate(note(REMOVE_LESSON))
    unused = await store.add_candidate(note(LAYOUT_LESSON))
    candidate = await store.add_candidate(note(FORMAT_LESSON))
    await store.promote(used, "alice")
    await store.promote(unused, "alice")
    assert (await row_of(sessions, used)).last_used_at is None

    await store.search(REPO, QUERY, k=1)
    first_use = (await row_of(sessions, used)).last_used_at
    await store.search(REPO, QUERY, k=1)

    row = await row_of(sessions, used)
    assert row.uses == 2
    assert row.last_used_at is not None and first_use is not None
    assert row.last_used_at >= first_use
    # Only the items that came back are counted, and a candidate is not one of them.
    assert (await row_of(sessions, unused)).uses == 0
    assert (await row_of(sessions, candidate)).uses == 0


async def test_a_search_for_no_items_changes_nothing(store: PgMemoryStore) -> None:
    assert await store.search(REPO, QUERY, k=5) == []


async def test_a_search_asks_for_at_least_one_item(store: PgMemoryStore) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        await store.search(REPO, QUERY, k=0)


@pytest.fixture
async def indexed_sessions(scratch: Scratch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on a table where Postgres can only answer a nearest-neighbour query from HNSW.

    A table of a few hundred rows is not the table that has the problem. The planner reads it
    whole, or finds the rows of one repo through the unique index, and returns the right rows
    either way. On a table of millions it picks the vector index, and so it does here once the
    other ways are closed: sequential scans are switched off for the sessions, and the unique
    index is dropped from this throwaway database. The search does not use it.
    """
    scratch.execute("DROP INDEX uq_memory_items_repo_kind_content_hash")
    engine = create_async_engine(
        to_async_url(scratch.url), connect_args={"server_settings": {"enable_seqscan": "off"}}
    )
    try:
        yield async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_a_filtered_search_returns_k_items_when_few_rows_pass_the_filter(
    indexed_sessions: async_sessionmaker[AsyncSession],
) -> None:
    # The index reads the nearest 40 rows of the table, and WHERE status = 'approved' is applied
    # to those afterwards. Here 300 candidates are nearer to the question than the 6 approved
    # notes, so a plain query finds none of them (ADR-0010, "To watch"). That is the shape of a
    # real repo: many proposals, few approvals.
    embedder = HashEmbedder()
    crowd = [
        f"Remove raises an error for a missing item in the inventory, case {i}." for i in range(300)
    ]
    approved = [f"Format the widget code with ruff, rule {i}." for i in range(6)]
    async with indexed_sessions.begin() as session:
        for status, texts in (("candidate", crowd), ("approved", approved)):
            for text, vector in zip(texts, embedder.embed(texts), strict=True):
                session.add(
                    MemoryItemRow(
                        repo=REPO,
                        kind="lesson",
                        content=text,
                        embedding=vector,
                        status=status,
                        source="human",
                        approved_by="alice" if status == "approved" else None,
                    )
                )

    [query] = embedder.embed([QUERY])
    plain = (
        select(MemoryItemRow.id)
        .where(MemoryItemRow.repo == REPO, MemoryItemRow.status == "approved")
        .order_by(MemoryItemRow.embedding.cosine_distance(query))
        .limit(5)
    )
    async with indexed_sessions() as session:
        # The control: the same query without the store's setting loses rows.
        assert len((await session.scalars(plain)).all()) < 5

    found = await PgMemoryStore(indexed_sessions, embedder).search(REPO, QUERY, k=5)

    assert len(found) == 5
    assert {item.status for item in found} == {"approved"}
    assert all(item.content in approved for item in found)


# ---------- approval ----------
async def test_promote_approves_the_item_and_names_who_did(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    item_id = await store.add_candidate(note(REMOVE_LESSON))

    await store.promote(item_id, "alice")

    row = await row_of(sessions, item_id)
    assert (row.status, row.approved_by) == ("approved", "alice")
    assert [i.id for i in await store.search(REPO, QUERY, k=5)] == [item_id]


async def test_promote_names_the_latest_approver_and_can_undo_a_reject(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    item_id = await store.add_candidate(note(REMOVE_LESSON))
    await store.reject(item_id, "alice")

    await store.promote(item_id, "bob")
    await store.promote(item_id, "carol")

    row = await row_of(sessions, item_id)
    assert (row.status, row.approved_by) == ("approved", "carol")


async def test_reject_keeps_the_item_but_takes_it_out_of_the_prompt(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession]
) -> None:
    item_id = await store.add_candidate(note(REMOVE_LESSON))
    await store.promote(item_id, "alice")

    await store.reject(item_id, "bob")

    row = await row_of(sessions, item_id)
    assert (row.status, row.approved_by) == ("rejected", None)
    assert await store.search(REPO, QUERY, k=5) == []


@pytest.mark.parametrize("action", ["promote", "reject"])
async def test_an_unknown_item_is_reported(store: PgMemoryStore, action: str) -> None:
    for item_id in (str(uuid.uuid4()), "not-a-uuid", ""):
        with pytest.raises(MemoryItemNotFoundError, match="no memory item"):
            await getattr(store, action)(item_id, "alice")


@pytest.mark.parametrize("action", ["promote", "reject"])
async def test_an_approval_names_a_person(
    store: PgMemoryStore, sessions: async_sessionmaker[AsyncSession], action: str
) -> None:
    item_id = await store.add_candidate(note(REMOVE_LESSON))
    for who in ("", "   "):
        with pytest.raises(ValueError, match="who"):
            await getattr(store, action)(item_id, who)
    assert (await row_of(sessions, item_id)).status == "candidate"


# ---------- listing ----------
async def test_list_items_stops_at_the_limit(store: PgMemoryStore) -> None:
    first = await store.add_candidate(note(REMOVE_LESSON))
    second = await store.add_candidate(note(FORMAT_LESSON, kind="pitfall"))
    await store.add_candidate(note(LAYOUT_LESSON))

    assert [e.id for e in await store.list_items(REPO, limit=2)] == [first, second]
    assert len(await store.list_items(REPO, limit=10)) == 3


async def test_list_items_filters_by_status_and_kind(store: PgMemoryStore) -> None:
    lesson = await store.add_candidate(note(REMOVE_LESSON))
    pitfall = await store.add_candidate(note(FORMAT_LESSON, kind="pitfall"))
    verified = await store.add_verified(
        note("pytest -q", kind="command", status="approved", source="default_branch"),
        source_ref=SHA,
    )
    await store.add_candidate(note(LAYOUT_LESSON, repo=OTHER_REPO))
    await store.reject(pitfall, "alice")

    everything = await store.list_items(REPO)
    assert [e.id for e in everything] == [lesson, pitfall, verified]
    assert [e.id for e in await store.list_items(REPO, status="candidate")] == [lesson]
    assert [e.id for e in await store.list_items(REPO, status="rejected")] == [pitfall]
    assert [e.id for e in await store.list_items(REPO, kind="command")] == [verified]
    assert await store.list_items(REPO, status="candidate", kind="command") == []

    entry = everything[2]
    assert (entry.repo, entry.kind, entry.status, entry.source) == (
        REPO,
        "command",
        "approved",
        "default_branch",
    )
    assert (entry.source_ref, entry.approved_by, entry.uses) == (SHA, DEFAULT_BRANCH_APPROVER, 0)
    assert entry.content == "pytest -q"
    assert entry.created_at is not None and entry.last_used_at is None


async def test_get_verified_finds_the_item_checked_at_a_sha(store: PgMemoryStore) -> None:
    other = "b" * 40
    at_sha = await store.add_verified(
        note('{"sha": "a"}', kind="repo_map", status="approved", source="default_branch"),
        source_ref=SHA,
    )
    await store.add_verified(
        note('{"sha": "b"}', kind="repo_map", status="approved", source="default_branch"),
        source_ref=other,
    )
    await store.add_verified(
        note("pytest -q", kind="command", status="approved", source="default_branch"),
        source_ref=SHA,
    )

    found = await store.get_verified(REPO, "repo_map", SHA)
    assert found is not None
    assert (found.id, found.content, found.source_ref) == (at_sha, '{"sha": "a"}', SHA)
    assert (found.kind, found.status, found.source) == ("repo_map", "approved", "default_branch")
    # Another repo, another kind of item and a sha that was never checked find nothing.
    assert await store.get_verified(OTHER_REPO, "repo_map", SHA) is None
    assert await store.get_verified(REPO, "command", other) is None
    assert await store.get_verified(REPO, "repo_map", "c" * 40) is None


async def test_get_verified_ignores_what_a_person_turned_down(store: PgMemoryStore) -> None:
    item_id = await store.add_verified(
        note("{}", kind="repo_map", status="approved", source="default_branch"), source_ref=SHA
    )
    await store.reject(item_id, "alice")

    assert await store.get_verified(REPO, "repo_map", SHA) is None


async def test_is_rejected_is_true_only_while_a_person_has_turned_the_item_down(
    store: PgMemoryStore,
) -> None:
    item_id = await store.add_verified(
        note("{}", kind="repo_map", status="approved", source="default_branch"), source_ref=SHA
    )
    assert not await store.is_rejected(REPO, "repo_map", SHA)

    await store.reject(item_id, "alice")

    assert await store.is_rejected(REPO, "repo_map", SHA)
    # Another sha, repo and kind are not affected.
    assert not await store.is_rejected(REPO, "repo_map", "b" * 40)
    assert not await store.is_rejected(OTHER_REPO, "repo_map", SHA)
    assert not await store.is_rejected(REPO, "command", SHA)
    await store.promote(item_id, "alice")
    assert not await store.is_rejected(REPO, "repo_map", SHA)


async def test_get_verified_returns_the_newest_of_two_items_at_one_sha(
    store: PgMemoryStore,
) -> None:
    # The same commit read by two versions of the parser gives two different notes.
    for text in ('{"version": 1}', '{"version": 2}'):
        await store.add_verified(
            note(text, kind="repo_map", status="approved", source="default_branch"),
            source_ref=SHA,
        )

    found = await store.get_verified(REPO, "repo_map", SHA)
    assert found is not None and found.content == '{"version": 2}'


# ---------- the migration ----------
def test_the_hash_is_one_step_on_the_migration_path(scratch: Scratch) -> None:
    cfg = scratch.config()
    revision = "0008_memory_content_hash"
    below = ScriptDirectory.from_config(cfg).get_revision(revision).down_revision
    assert below == "0007_memory_items"
    index = "uq_memory_items_repo_kind_content_hash"

    def indexdef() -> list[tuple[Any, ...]]:
        return scratch.query("SELECT indexdef FROM pg_indexes WHERE indexname = %s", index)

    assert "UNIQUE INDEX" in indexdef()[0][0]
    assert "(repo, kind, content_hash)" in indexdef()[0][0]

    command.downgrade(cfg, below)
    assert indexdef() == []
    columns = scratch.query(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'memory_items'"
    )
    assert ("content_hash",) not in columns

    command.upgrade(cfg, "head")
    assert len(indexdef()) == 1


def test_rows_written_without_a_hash_do_not_collide(scratch: Scratch) -> None:
    # Rows that something other than the store wrote have no hash. NULLs are all different in
    # a unique index, so they never stop each other.
    vector = Vector(HashEmbedder().embed(["x"])[0]).to_text()
    insert = (
        "INSERT INTO memory_items (repo, kind, content, embedding, source) "
        "VALUES (%s, 'lesson', 'x', %s, 'task')"
    )
    with psycopg.connect(scratch.url) as conn:
        conn.execute(insert, (REPO, vector))
        conn.execute(insert, (REPO, vector))
    assert scratch.query("SELECT count(*) FROM memory_items") == [(2,)]
