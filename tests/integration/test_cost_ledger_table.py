"""The cost_ledger migration (LH-701).

tests/integration/test_migrations.py already checks that the migrated schema equals the models.
These tests check what only this table adds: its place on the migration path, its defaults, its
required columns and the rows it has to take. Each test works on a throwaway database that it
creates and drops.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
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

pytestmark = [pytest.mark.integration, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_migtest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REVISION = "0006_cost_ledger"
COLUMNS = (
    "id, task_id, repo, model, turn_index, attempt, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, usd, ts"
)


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
    columns: dict[str, Any] = {
        "task_id": "lh-acme-calc-42",
        "repo": "acme/calc",
        "model": "claude-sonnet-5-5",
        "turn_index": 3,
        "attempt": 1,
        "usd": Decimal("0.004350"),
    }
    columns.update(overrides)
    names = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    query = sql.SQL("INSERT INTO cost_ledger ({}) VALUES ({}) RETURNING {}").format(
        names, marks, sql.SQL(COLUMNS)
    )
    row = conn.execute(query, list(columns.values())).fetchone()
    assert row is not None
    return row


def test_the_table_is_one_step_on_the_migration_path(scratch: Scratch) -> None:
    cfg = scratch.config()
    # The step below this one is read from the scripts, so that a migration chained in front
    # of it later does not break the test.
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision(REVISION)
    assert revision is not None
    below = revision.down_revision
    assert isinstance(below, str)

    command.upgrade(cfg, "head")
    assert scratch.tables() >= {"side_effects", "cost_ledger"}

    command.downgrade(cfg, below)
    assert "cost_ledger" not in scratch.tables()
    assert "turns" in scratch.tables()

    command.upgrade(cfg, "head")
    assert "cost_ledger" in scratch.tables()


def test_a_row_takes_the_defaults(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")

    with psycopg.connect(scratch.url) as conn:
        first = _insert(conn)
        second = _insert(conn, input_tokens=10)

    assert (first[0], second[0]) == (1, 2)  # id counts up by itself
    assert first[6:10] == (0, 0, 0, 0)  # the token counts default to zero
    assert second[6] == 10
    assert first[10] == Decimal("0.004350")
    assert first[11] is not None  # ts has a default


def test_a_turn_can_have_many_rows_with_the_same_attempt(scratch: Scratch) -> None:
    # A reply cut off at max_tokens is asked again by a new activity run, so the same task,
    # turn and attempt come back with another call. The ledger keeps both.
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn:
        _insert(conn)
        _insert(conn)
        (count,) = conn.execute("SELECT count(*) FROM cost_ledger").fetchone() or (0,)
    assert count == 2


def test_the_usd_column_keeps_millionths_of_a_dollar(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn:
        row = _insert(conn, usd=Decimal("1234.567891"))
    assert row[10] == Decimal("1234.567891")


@pytest.mark.parametrize("missing", ["task_id", "repo", "model", "turn_index", "attempt", "usd"])
def test_the_required_columns_are_required(scratch: Scratch, missing: str) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn, pytest.raises(errors.NotNullViolation):
        _insert(conn, **{missing: None})


def test_the_rows_are_found_by_task_and_by_repo_and_day(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn:
        rows = conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename = 'cost_ledger'"
        ).fetchall()
    assert {"ix_cost_ledger_task_id", "ix_cost_ledger_repo_ts", "pk_cost_ledger"} == {
        row[0] for row in rows
    }
