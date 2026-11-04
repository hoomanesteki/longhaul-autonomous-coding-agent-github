"""The memory_items table, its migration and its vector index (LH-601).

tests/integration/test_migrations.py already checks that the migrated schema equals the models.
These tests check what only this table adds: its step on the migration path, its constraints
and defaults, and that Postgres finds the nearest neighbour by cosine distance, through the
HNSW index and without it. Each test works on a throwaway database that it creates and drops.
"""

from __future__ import annotations

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
from psycopg import errors, sql
from psycopg.rows import dict_row
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.db.models import MemoryItemRow
from longhaul.db.session import make_engine
from longhaul.memory.embedder import HashEmbedder
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_memtest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REVISION = "0007_memory_items"
INDEX = "ix_memory_items_embedding"
REPO = "acme/inventory"
KINDS = ("repo_map", "command", "convention", "pitfall", "lesson")
STATUSES = ("candidate", "approved", "rejected")
SOURCES = ("default_branch", "merged_pr", "human", "task")

# Their distances from QUERY are 0.58, 0.74, 0.83, 0.87 and 0.91, so the order of the nearest
# three does not hang on a tie that Postgres may break either way.
LESSONS = [
    "Raise InventoryError, not ValueError, when an item is missing from the inventory.",
    "Check that remove raises the error with pytest.raises.",
    "Format the code with ruff and keep lines under 100 characters.",
    "Name test files test_<module>.py and keep one test file per module.",
    "Run pytest -q from the repo root before you call finish.",
]
QUERY = "Which error does remove raise for a missing item in the inventory?"


class Scratch:
    def __init__(self, name: str, url: str) -> None:
        self.name, self.url = name, url

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def query(self, statement: LiteralString) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement).fetchall()

    def tables(self) -> set[str]:
        rows = self.query("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in rows}


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
        yield Scratch(name, url)
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def migrated(scratch: Scratch) -> Scratch:
    command.upgrade(scratch.config(), "head")
    return scratch


@pytest.fixture
async def sessions(migrated: Scratch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(migrated.url)
    try:
        yield async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _text(vector: list[float]) -> str:
    """A vector as pgvector reads it from text, for the raw SQL below."""
    return Vector(vector).to_text()


def _insert(conn: psycopg.Connection[Any], **overrides: Any) -> dict[str, Any]:
    columns: dict[str, Any] = {
        "repo": REPO,
        "kind": "lesson",
        "content": LESSONS[0],
        "embedding": _text(HashEmbedder().embed([LESSONS[0]])[0]),
        "source": "task",
    }
    columns.update(overrides)
    names = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    query = sql.SQL("INSERT INTO memory_items ({}) VALUES ({}) RETURNING *").format(names, marks)
    row = conn.execute(query, list(columns.values())).fetchone()
    assert row is not None
    return row


def _item(text: str, vector: list[float], **fields: Any) -> MemoryItemRow:
    values: dict[str, Any] = {
        "repo": REPO,
        "kind": "lesson",
        "status": "approved",
        "source": "merged_pr",
        "source_ref": "42",
    }
    values.update(fields)
    return MemoryItemRow(content=text, embedding=vector, **values)


# ---------- the vectors ----------
async def test_insert_and_knn(sessions: async_sessionmaker[AsyncSession]) -> None:
    embedder = HashEmbedder()
    vectors = embedder.embed(LESSONS)
    async with sessions.begin() as session:
        session.add_all(_item(t, v) for t, v in zip(LESSONS, vectors, strict=True))

    [query] = embedder.embed([QUERY])
    distance = MemoryItemRow.embedding.cosine_distance(query).label("distance")
    nearest = select(MemoryItemRow.content, distance).order_by(distance).limit(3)
    async with sessions() as session:
        found = (await session.execute(nearest)).all()

    # The same ranking worked out in Python. For vectors of unit length the cosine distance
    # is one minus the dot product.
    ranked = sorted(
        (1 - sum(q * v for q, v in zip(query, vector, strict=True)), text)
        for text, vector in zip(LESSONS, vectors, strict=True)
    )
    assert [row.content for row in found] == [text for _, text in ranked[:3]]
    assert [row.distance for row in found] == pytest.approx([d for d, _ in ranked[:3]], abs=1e-5)
    assert found[0].content == LESSONS[0]  # the lesson that is about the question


async def test_a_stored_vector_reads_back_as_written(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    [vector] = HashEmbedder().embed([LESSONS[1]])
    async with sessions.begin() as session:
        session.add(_item(LESSONS[1], vector))

    async with sessions() as session:
        stored = (await session.scalars(select(MemoryItemRow))).one()

    assert len(stored.embedding) == len(vector)
    # The column holds 32 bit floats, so the copy is close and not equal.
    assert stored.embedding == pytest.approx(vector, abs=1e-6)


async def test_the_nearest_items_of_one_repo_and_status_come_first(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    # The query retrieval will run: approved items of one repo, nearest first.
    embedder = HashEmbedder()
    [near, far] = embedder.embed([LESSONS[0], LESSONS[2]])
    async with sessions.begin() as session:
        session.add_all(
            [
                _item(LESSONS[0], near, source_ref="approved, near"),
                _item(LESSONS[0], near, source_ref="candidate", status="candidate"),
                _item(LESSONS[0], near, source_ref="other repo", repo="acme/calc"),
                _item(LESSONS[2], far, source_ref="approved, far"),
            ]
        )

    distance = MemoryItemRow.embedding.cosine_distance(near)
    stmt = (
        select(MemoryItemRow.source_ref)
        .where(MemoryItemRow.repo == REPO, MemoryItemRow.status == "approved")
        .order_by(distance)
        .limit(5)
    )
    async with sessions() as session:
        found = (await session.scalars(stmt)).all()

    assert list(found) == ["approved, near", "approved, far"]


def test_the_embedding_index_is_hnsw_on_cosine_distance(migrated: Scratch) -> None:
    rows = migrated.query(
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'memory_items' AND indexname = "
        f"'{INDEX}'"
    )
    assert len(rows) == 1
    assert "USING hnsw (embedding vector_cosine_ops)" in rows[0][0]


def test_a_cosine_query_can_be_answered_by_the_index(migrated: Scratch) -> None:
    # With a few rows the planner reads the table in order, whatever indexes exist. Switching
    # sequential scans off shows whether the index is able to answer the query at all, and an
    # index built with another operator class (the L2 one) would not be.
    embedder = HashEmbedder()
    [query] = embedder.embed([QUERY])
    with psycopg.connect(migrated.url, row_factory=dict_row) as conn:
        for text, vector in zip(LESSONS, embedder.embed(LESSONS), strict=True):
            _insert(conn, content=text, embedding=_text(vector))
        conn.execute("SET enable_seqscan = off")
        plan = conn.execute(
            "EXPLAIN SELECT id FROM memory_items ORDER BY embedding <=> %s::vector LIMIT 3",
            [_text(query)],
        ).fetchall()

    lines = [row["QUERY PLAN"] for row in plan]
    assert any(f"Index Scan using {INDEX} on memory_items" in line for line in lines), lines


# ---------- the migration ----------
def test_the_table_is_one_step_on_the_migration_path(scratch: Scratch) -> None:
    cfg = scratch.config()
    # The step below this one is read from the scripts, so that a migration chained in front
    # of it later does not break the test.
    below = ScriptDirectory.from_config(cfg).get_revision(REVISION).down_revision
    assert isinstance(below, str)

    command.upgrade(cfg, "head")
    assert "memory_items" in scratch.tables()

    command.downgrade(cfg, below)
    assert "memory_items" not in scratch.tables()
    assert scratch.query(f"SELECT 1 FROM pg_indexes WHERE indexname = '{INDEX}'") == []
    # The extension stays, as 0001_baseline says it does: later objects may depend on it.
    assert ("vector",) in scratch.query("SELECT extname FROM pg_extension")

    command.upgrade(cfg, "head")
    assert "memory_items" in scratch.tables()


# ---------- the rows ----------
def test_a_new_row_gets_an_id_and_starts_as_a_candidate(migrated: Scratch) -> None:
    with psycopg.connect(migrated.url, row_factory=dict_row) as conn:
        first = _insert(conn)
        second = _insert(conn)

    assert isinstance(first["id"], uuid.UUID)
    assert first["id"] != second["id"]
    assert (first["status"], first["uses"]) == ("candidate", 0)
    assert first["created_at"] is not None
    assert (first["source_ref"], first["approved_by"], first["last_used_at"]) == (None,) * 3


def test_every_kind_status_and_source_of_the_plan_is_accepted(migrated: Scratch) -> None:
    with psycopg.connect(migrated.url, row_factory=dict_row) as conn:
        kinds = [_insert(conn, kind=kind)["kind"] for kind in KINDS]
        statuses = [_insert(conn, status=status)["status"] for status in STATUSES]
        sources = [_insert(conn, source=source)["source"] for source in SOURCES]

    assert (kinds, statuses, sources) == (list(KINDS), list(STATUSES), list(SOURCES))


@pytest.mark.parametrize(
    ("column", "value"),
    [("kind", "note"), ("status", "pending"), ("source", "model")],
)
def test_a_value_outside_the_plan_is_refused(migrated: Scratch, column: str, value: str) -> None:
    with (
        psycopg.connect(migrated.url, row_factory=dict_row) as conn,
        pytest.raises(errors.CheckViolation, match=f"ck_memory_items_{column}"),
    ):
        _insert(conn, **{column: value})


@pytest.mark.parametrize("missing", ["repo", "kind", "content", "embedding", "status", "source"])
def test_the_required_columns_are_required(migrated: Scratch, missing: str) -> None:
    with (
        psycopg.connect(migrated.url, row_factory=dict_row) as conn,
        pytest.raises(errors.NotNullViolation),
    ):
        _insert(conn, **{missing: None})


def test_a_vector_of_the_wrong_width_is_refused(migrated: Scratch) -> None:
    # The width is part of the type, so a second embedder with another width cannot write
    # into this column by mistake.
    with (
        psycopg.connect(migrated.url, row_factory=dict_row) as conn,
        pytest.raises(errors.DataException, match="expected 384 dimensions, not 3"),
    ):
        _insert(conn, embedding="[1,2,3]")
