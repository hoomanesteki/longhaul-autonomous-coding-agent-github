"""The side_effects migration (LH-304).

tests/integration/test_migrations.py already checks that the migrated schema equals the models.
These tests check what only this table adds: its place on the migration path, its constraint,
its defaults and its primary key. Each test works on a throwaway database that it creates and
drops.
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
from sqlalchemy.engine import make_url

from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_migtest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REVISION = "0003_lh304_side_effects"
KINDS = ("comment", "label_add", "label_remove", "pr", "push")


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
    columns = {
        "key": "lh-acme-calc-42:picked_up:1",
        "task_id": "lh-acme-calc-42",
        "kind": "comment",
        "external_id": "1000123",
        "payload_hash": "a" * 64,
    }
    columns.update(overrides)
    names = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    query = sql.SQL("INSERT INTO side_effects ({}) VALUES ({}) RETURNING *").format(names, marks)
    row = conn.execute(query, list(columns.values())).fetchone()
    assert row is not None
    return row


def test_the_table_is_one_step_on_the_migration_path(scratch: Scratch) -> None:
    cfg = scratch.config()
    # The step below this one is read from the scripts, so that a migration chained in front
    # of it later does not break the test.
    below = ScriptDirectory.from_config(cfg).get_revision(REVISION).down_revision
    assert isinstance(below, str)

    command.upgrade(cfg, "head")
    assert scratch.tables() >= {"checkpoints", "side_effects"}

    command.downgrade(cfg, below)
    assert "side_effects" not in scratch.tables()
    assert "checkpoints" in scratch.tables()

    command.upgrade(cfg, "head")
    assert "side_effects" in scratch.tables()


def test_a_row_takes_the_five_kinds_and_a_default_time(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")

    with psycopg.connect(scratch.url) as conn:
        rows = [_insert(conn, key=f"lh-acme-calc-42:{kind}:1", kind=kind) for kind in KINDS]

    assert [row[2] for row in rows] == list(KINDS)
    assert all(row[5] is not None for row in rows)  # created_at has a default


def test_a_kind_outside_the_plan_is_refused(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn, pytest.raises(errors.CheckViolation):
        _insert(conn, kind="merge")


def test_a_key_can_be_recorded_only_once(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn:
        _insert(conn)
        with pytest.raises(errors.UniqueViolation):
            _insert(conn, external_id="other")


@pytest.mark.parametrize("missing", ["key", "task_id", "kind", "external_id", "payload_hash"])
def test_the_required_columns_are_required(scratch: Scratch, missing: str) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn, pytest.raises(errors.NotNullViolation):
        _insert(conn, **{missing: None})
