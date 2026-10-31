"""The checkpoints migration (LH-205).

tests/integration/test_migrations.py already checks that the migrated schema equals the models.
These tests check what only this table adds: its constraint, its defaults and its step on the
migration path. Each test works on a throwaway database that it creates and drops.
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
from psycopg import errors, sql
from psycopg.types.json import Jsonb
from sqlalchemy.engine import make_url

from longhaul.contracts import BlobRef
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_migtest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
BASELINE = {"repo_settings", "tasks"}
KINDS = ("tool", "step", "compaction", "bundle")


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
    columns = {"task_id": "lh-acme-calc-42", "sha": "a" * 40, "kind": "tool", "turn_index": 3}
    columns.update(overrides)
    names = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    query = sql.SQL("INSERT INTO checkpoints ({}) VALUES ({}) RETURNING *").format(names, marks)
    row = conn.execute(query, list(columns.values())).fetchone()
    assert row is not None
    return row


def test_the_table_is_one_step_on_the_migration_path(scratch: Scratch) -> None:
    cfg = scratch.config()

    command.upgrade(cfg, "head")
    assert scratch.tables() >= BASELINE | {"checkpoints"}

    command.downgrade(cfg, "0001_baseline")
    assert "checkpoints" not in scratch.tables()
    assert scratch.tables() >= BASELINE

    command.upgrade(cfg, "head")
    assert "checkpoints" in scratch.tables()


def test_rows_take_the_four_kinds_and_the_defaults(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    ref = BlobRef(key="tasks/lh-acme-calc-42/ckpt/" + "a" * 40 + ".bundle", sha256="b" * 64, size=9)

    with psycopg.connect(scratch.url) as conn:
        rows = [_insert(conn, kind=kind) for kind in KINDS]
        bundled = _insert(conn, kind="bundle", bundle_ref=Jsonb(ref.model_dump()))

    assert [row[0] for row in rows] == [1, 2, 3, 4]  # id counts up by itself
    assert [row[3] for row in rows] == list(KINDS)
    first = rows[0]
    assert first[4] is None and first[6] is None  # step_id and bundle_ref are optional
    assert first[7] is not None  # created_at has a default
    assert BlobRef.model_validate(bundled[6]) == ref


def test_a_kind_outside_the_plan_is_refused(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn, pytest.raises(errors.CheckViolation):
        _insert(conn, kind="snapshot")


@pytest.mark.parametrize("missing", ["task_id", "sha", "kind", "turn_index"])
def test_the_required_columns_are_required(scratch: Scratch, missing: str) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn, pytest.raises(errors.NotNullViolation):
        _insert(conn, **{missing: None})
