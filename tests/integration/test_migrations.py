"""Migration tests.

Every test works on a throwaway database that the test creates and drops. A downgrade drops
every table, so none of these tests may ever point alembic at the application database.
"""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from psycopg import sql
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from longhaul.db.models import Base, RepoSetting, Task
from longhaul.db.session import make_engine
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_migtest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect,
# instead of reaching a real database.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
WANTED_TABLES = {"repo_settings", "tasks"}


def _server_url() -> Any:
    """The configured server. It is only used to create and drop scratch databases."""
    secret = EnvSettings().database_url
    return make_url(secret.get_secret_value() if secret else LOCAL_DSN)


def _dsn(url: Any) -> str:
    return str(url.render_as_string(hide_password=False))


@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def config(self, *, with_url: bool = True) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        if with_url:
            cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def query(self, statement: LiteralString) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement).fetchall()

    def tables(self) -> set[str]:
        rows = self.query("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        return {row[0] for row in rows}

    def downgrade_to_base(self) -> None:
        # A downgrade drops every table, so it only ever runs on a scratch database.
        assert self.name.startswith(SCRATCH_PREFIX)
        command.downgrade(self.config(), "base")

    def schema_diff(self, *statements: str) -> list[Any]:
        """What alembic would change to make the database match models.py.

        Statements run first, inside the same transaction, which is rolled back afterwards.
        """
        engine = create_engine(_dsn(make_url(self.url).set(drivername="postgresql+psycopg")))
        try:
            with engine.connect() as conn:
                with conn.begin() as transaction:
                    for statement in statements:
                        conn.exec_driver_sql(statement)
                    context = MigrationContext.configure(
                        conn, opts={"compare_type": True, "compare_server_default": True}
                    )
                    diff = compare_metadata(context, Base.metadata)
                    transaction.rollback()
                return diff
        finally:
            engine.dispose()


@pytest.fixture
def make_scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], Scratch]]:
    server = _server_url()
    admin_dsn = _dsn(server.set(database="postgres"))
    created: list[str] = []

    def make() -> Scratch:
        name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
        assert name != server.database
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return Scratch(name=name, url=_dsn(server.set(database=name)))

    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    try:
        yield make
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            for name in created:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


@pytest.fixture
def scratch(make_scratch: Callable[[], Scratch]) -> Scratch:
    return make_scratch()


def test_upgrade_downgrade_roundtrip(scratch: Scratch) -> None:
    cfg = scratch.config()

    command.upgrade(cfg, "head")
    assert scratch.tables() >= WANTED_TABLES

    command.upgrade(cfg, "head")  # a second run changes nothing
    assert scratch.tables() >= WANTED_TABLES

    scratch.downgrade_to_base()
    assert not scratch.tables() & WANTED_TABLES

    command.upgrade(cfg, "head")
    assert scratch.tables() >= WANTED_TABLES


def test_migrated_schema_matches_models(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")

    assert scratch.schema_diff() == []


def test_vector_extension_and_indexes_exist(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")

    extensions = scratch.query("SELECT extname FROM pg_extension")
    assert ("vector",) in extensions
    indexes = scratch.query("SELECT tablename, indexname FROM pg_indexes WHERE schemaname='public'")
    assert {("tasks", "ix_tasks_repo"), ("tasks", "ix_tasks_status")} <= set(indexes)
    constraints = scratch.query("SELECT conname FROM pg_constraint WHERE contype = 'p'")
    assert {("pk_tasks",), ("pk_repo_settings",)} <= set(constraints)


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("ALTER TABLE tasks DROP COLUMN pr_url", id="column removed"),
        pytest.param("ALTER TABLE tasks ALTER COLUMN title DROP NOT NULL", id="nullability"),
        pytest.param("ALTER TABLE tasks ALTER COLUMN cost_usd TYPE numeric(10, 2)", id="type"),
        pytest.param("ALTER TABLE tasks ALTER COLUMN turns SET DEFAULT 5", id="changed default"),
        pytest.param(
            "ALTER TABLE repo_settings ALTER COLUMN created_at DROP DEFAULT", id="default dropped"
        ),
        pytest.param("DROP INDEX ix_tasks_status", id="index removed"),
    ],
)
def test_schema_comparison_detects_drift(scratch: Scratch, statement: str) -> None:
    # Guards the guard: each statement is a change that a hand-edited migration could make.
    command.upgrade(scratch.config(), "head")

    assert scratch.schema_diff(statement) != []


def test_orm_round_trip_uses_the_server_defaults(scratch: Scratch) -> None:
    command.upgrade(scratch.config(), "head")
    engine = create_engine(_dsn(make_url(scratch.url).set(drivername="postgresql+psycopg")))
    try:
        with Session(engine) as session:
            session.add(RepoSetting(repo="acme/calc"))
            session.add(
                Task(
                    id="lh-acme-calc-42",
                    repo="acme/calc",
                    issue_number=42,
                    title="add subtracts",
                    status="running",
                    phase="coding",
                )
            )
            session.commit()
        with Session(engine) as session:
            repo = session.get(RepoSetting, "acme/calc")
            task = session.get(Task, "lh-acme-calc-42")
            assert repo is not None
            assert (repo.allowlisted, repo.settings) == (False, {})
            assert repo.created_at is not None
            assert task is not None
            assert (task.cost_usd, task.turns, task.settings) == (Decimal("0"), 0, {})
            assert task.created_at is not None
    finally:
        engine.dispose()


def test_downgrade_never_reaches_the_application_database(
    make_scratch: Callable[[], Scratch], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stand-in for the developer's own database: LONGHAUL_DATABASE_URL points at it, and the
    # roundtrip below must leave its tables and rows alone.
    application = make_scratch()
    command.upgrade(application.config(), "head")
    with psycopg.connect(application.url) as conn:
        conn.execute("INSERT INTO repo_settings (repo) VALUES ('acme/real-repo')")
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", application.url)
    get_settings.cache_clear()

    throwaway = make_scratch()
    command.upgrade(throwaway.config(), "head")
    throwaway.downgrade_to_base()

    assert application.query("SELECT repo FROM repo_settings") == [("acme/real-repo",)]


async def test_upgrade_runs_on_a_given_connection_inside_a_running_loop(scratch: Scratch) -> None:
    # Alembic cannot start a second event loop here, so the caller hands it a connection.
    # The config has no URL, and the environment URL is unreachable, so only the connection
    # can explain a successful upgrade.
    cfg = scratch.config(with_url=False)

    def upgrade(connection: Any) -> None:
        cfg.attributes["connection"] = connection
        command.upgrade(cfg, "head")

    engine = make_engine(scratch.url, pooled=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(upgrade)
    finally:
        await engine.dispose()

    assert scratch.tables() >= WANTED_TABLES
