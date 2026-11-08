"""The plans migration and the SQL side of store_plan (LH-501).

tests/integration/test_migrations.py already checks that the migrated schema equals the models.
These tests check what only this table adds: its place on the migration path, that it drops
cleanly, its defaults and constraints, and that the SQL recorder writes a plan the way the memory
recorder does. Each test works on a throwaway database that it creates and drops.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import ActivityEnvironment

from longhaul.contracts import Plan, PlanStep
from longhaul.db.records import PlanRecord, SqlRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.plan import PlanActivities
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_plantest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REVISION = "0011_plans"
TASK = "lh-acme-calc-42"
PLAN = Plan(
    summary="add() subtracts",
    steps=[
        PlanStep(id="s1", description="Fix add()", files=["calc.py"], done_when="add(2, 3) is 5"),
        PlanStep(id="s2", description="Test it", done_when="pytest -q passes"),
    ],
    acceptance_checks=["pytest -q"],
    risks=["none known"],
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

    def rows(self, statement: str) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement).fetchall()  # type: ignore[arg-type]


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
    # Alembic starts its own event loop, so it runs here and not inside an async fixture.
    command.upgrade(scratch.config(), "head")
    return scratch


@pytest.fixture
async def recorder(migrated: Scratch) -> AsyncIterator[SqlRecorder]:
    engine = make_engine(migrated.url)
    try:
        yield SqlRecorder(async_sessionmaker[AsyncSession](engine, expire_on_commit=False))
    finally:
        await engine.dispose()


def _insert(conn: psycopg.Connection[Any], **overrides: Any) -> tuple[Any, ...]:
    columns: dict[str, Any] = {"task_id": TASK, "version": 1, "plan": Jsonb({"summary": "s"})}
    columns.update(overrides)
    names = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
    marks = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
    query = sql.SQL("INSERT INTO plans ({}) VALUES ({}) RETURNING *").format(names, marks)
    row = conn.execute(query, list(columns.values())).fetchone()
    assert row is not None
    return row


# ---------- the migration ----------
def test_the_table_is_on_the_migration_path_and_the_history_has_one_head(scratch: Scratch) -> None:
    cfg = scratch.config()
    script = ScriptDirectory.from_config(cfg)
    revision = script.get_revision(REVISION)
    assert revision is not None
    # The merge step re-points the parent when other stories add migrations, so only the path
    # is checked here: one head, and the chain from the baseline passes through this step.
    assert len(script.get_heads()) == 1, "the history has one head"
    assert REVISION in {step.revision for step in script.walk_revisions()}
    assert revision.down_revision is not None

    command.upgrade(cfg, "head")
    assert "plans" in scratch.tables()

    command.downgrade(cfg, str(revision.down_revision))
    assert "plans" not in scratch.tables()
    assert "memory_items" in scratch.tables()

    command.upgrade(cfg, "head")
    assert "plans" in scratch.tables()


def test_a_row_has_defaults_and_the_key_is_the_task_and_the_version(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn:
        first = _insert(conn)
        second = _insert(conn, version=2)
        with pytest.raises(errors.UniqueViolation):
            _insert(conn)

    # task_id, version, plan, status, decided_by, decided_at, created_at
    assert first[3] == "proposed"
    assert first[4] is None and first[5] is None
    assert first[6] is not None
    assert second[1] == 2


@pytest.mark.parametrize("missing", ["task_id", "version", "plan", "status", "created_at"])
def test_the_required_columns_are_required(scratch: Scratch, missing: str) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn, pytest.raises(errors.NotNullViolation):
        _insert(conn, **{missing: None})


@pytest.mark.parametrize("state", ["proposed", "approved", "rejected", "superseded"])
def test_the_four_states_of_the_plan_are_accepted(scratch: Scratch, state: str) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn:
        assert _insert(conn, status=state)[3] == state


def test_any_other_state_is_refused(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    with psycopg.connect(scratch.url) as conn, pytest.raises(errors.CheckViolation):
        _insert(conn, status="maybe")


# ---------- the recorder ----------
async def test_a_plan_round_trips_as_json(migrated: Scratch, recorder: SqlRecorder) -> None:
    await recorder.record_plan(PlanRecord(TASK, 1, PLAN))

    ((task_id, version, plan, status, decided_by, decided_at),) = migrated.rows(
        "SELECT task_id, version, plan, status, decided_by, decided_at FROM plans"
    )
    assert (task_id, version, status, decided_by, decided_at) == (TASK, 1, "proposed", None, None)
    assert Plan.model_validate(plan) == PLAN


async def test_recording_a_plan_again_keeps_one_row(
    migrated: Scratch, recorder: SqlRecorder
) -> None:
    await recorder.record_plan(PlanRecord(TASK, 1, PLAN))
    await recorder.record_plan(PlanRecord(TASK, 1, PLAN))
    assert migrated.rows("SELECT count(*) FROM plans") == [(1,)]


async def test_a_row_that_has_been_decided_keeps_its_decision(
    migrated: Scratch, recorder: SqlRecorder
) -> None:
    # LH-502 approves a plan. A late retry of store_plan must not undo that.
    await recorder.record_plan(PlanRecord(TASK, 1, PLAN))
    with psycopg.connect(migrated.url) as conn:
        conn.execute("UPDATE plans SET status = 'approved', decided_by = 'alice'")
    await recorder.record_plan(PlanRecord(TASK, 1, PLAN))

    assert migrated.rows("SELECT status, decided_by FROM plans") == [("approved", "alice")]


async def test_store_plan_writes_to_postgres(migrated: Scratch, recorder: SqlRecorder) -> None:
    await ActivityEnvironment().run(
        PlanActivities(recorder).store_plan,
        acts.StorePlanInput(task_id=TASK, version=1, plan=PLAN),
    )
    assert migrated.rows("SELECT task_id, version FROM plans") == [(TASK, 1)]
