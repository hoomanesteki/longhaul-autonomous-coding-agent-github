"""The events migration (LH-206).

tests/integration/test_migrations.py already checks that the migrated schema equals the models.
These tests check what only this table adds: its place on the migration path, its defaults, its
required columns and that its type column takes any text. Each test works on a throwaway
database that it creates and drops.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import errors, sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_migtest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REVISION = "0005_events"
# The types that section 3.5 lists. The column takes any text, so later stories can add more.
TYPES = (
    "phase_changed", "paused", "resumed", "hint", "budget_added", "stuck", "compaction",
    "verdict", "continue_as_new", "sandbox_recreated", "cancelled",
)  # fmt: skip


class Scratch:
    def __init__(self, name: str, url: str) -> None:
        self.name, self.url = name, url

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def tables(self) -> set[str]:
        with psycopg.connect(self.url) as conn:
            rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            return {row[0] for row in rows.fetchall()}

    def indexes(self) -> set[tuple[str, str]]:
        with psycopg.connect(self.url) as conn:
            rows = conn.execute(
                "SELECT tablename, indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
            return {(row[0], row[1]) for row in rows.fetchall()}


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


def _insert(conn: psycopg.Connection[Any], **overrides: Any) -> tuple[Any, ...]:
    columns = {"task_id": "lh-acme-calc-42", "type": "sandbox_recreated"}
    columns.update(overrides)
    names = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    query = sql.SQL("INSERT INTO events ({}) VALUES ({}) RETURNING *").format(names, marks)
    row = conn.execute(query, list(columns.values())).fetchone()
    assert row is not None
    return row


def test_the_table_is_one_step_on_the_migration_path(scratch: Scratch) -> None:
    cfg = scratch.config()
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision(REVISION)
    assert revision is not None
    assert revision.down_revision == "0004_side_effects"
    # Later migrations chain on top of this one, so it is on the path, not always the head.
    assert len(script.get_heads()) == 1, "the history has one head"
    assert REVISION in {step.revision for step in script.walk_revisions()}

    command.upgrade(cfg, "head")
    assert scratch.tables() >= {"side_effects", "events"}

    command.downgrade(cfg, "0004_side_effects")
    assert "events" not in scratch.tables()
    assert "side_effects" in scratch.tables()

    command.upgrade(cfg, "head")
    assert "events" in scratch.tables()


def test_rows_are_found_by_task(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    assert ("events", "ix_events_task_id") in scratch.indexes()


def test_a_row_takes_any_type_and_has_defaults(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")

    with psycopg.connect(scratch.url) as conn:
        rows = [_insert(conn, type=kind) for kind in (*TYPES, "memory_promoted")]

    assert [row[3] for row in rows] == [*TYPES, "memory_promoted"]
    assert [row[0] for row in rows] == list(range(1, len(rows) + 1))  # id counts up by itself
    first = rows[0]
    assert first[2] is not None  # ts has a default
    assert first[4] == {}  # so does the payload
    assert first[5] is not None  # and created_at


def test_the_payload_keeps_nested_json(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    payload = {"head_sha": "a" * 40, "old": None, "counts": [1, 2], "inner": {"ok": True}}

    with psycopg.connect(scratch.url) as conn:
        row = _insert(conn, payload=Jsonb(payload))

    assert row[4] == payload


@pytest.mark.parametrize("missing", ["task_id", "type", "ts", "payload", "created_at"])
def test_the_required_columns_are_required(scratch: Scratch, missing: str) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn, pytest.raises(errors.NotNullViolation):
        _insert(conn, **{missing: None})
