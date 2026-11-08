"""What `longhaul status` reads of the context of a task (LH-407), against Postgres.

A task of the poller has its totals in its `tasks` row, and a task of `longhaul run` has no row
(docs/runbook.md). Both have to show the compactions and the largest request, so the reader takes
the row when there is one and the log of the task when there is not. The log is written here by
the recorder that the worker uses, so the test reads what a run really leaves.

Every test works on a throwaway database that the test creates and drops, like the other table
tests do.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul import cli
from longhaul.contracts import Usage
from longhaul.db.context_totals import ContextTotals, read_context_totals
from longhaul.db.records import SqlRecorder, TurnRecord
from longhaul.db.session import make_engine
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase4]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_statustest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
TASK = "lh-local-longlog-0a1b2c3d"
WINDOW = 8_000
STARTED = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def execute(self, statement: LiteralString, *params: Any) -> None:
        with psycopg.connect(self.url) as conn:
            conn.execute(statement, params)

    def add_task_row(self, task_id: str, compactions: int, max_fraction: float) -> None:
        self.execute(
            "INSERT INTO tasks (id, repo, title, status, phase, compactions, max_fraction) "
            "VALUES (%s, 'acme/calc', 'a task', 'active', 'queued', %s, %s)",
            task_id,
            compactions,
            max_fraction,
        )


@pytest.fixture
def scratch(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
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
        command.upgrade(Scratch(name, url).config(), "head")
        yield Scratch(name, url)
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


async def log_turn(
    recorder: SqlRecorder, index: int, role: str = "coder", *, tokens: int, cached: int = 0
) -> None:
    await recorder.record_turn(
        TurnRecord(
            task_id=TASK,
            turn_index=index,
            attempt=1,
            role=role,
            started_at=STARTED,
            usage=Usage(input_tokens=tokens - cached, cache_read_tokens=cached),
        )
    )


async def log_compaction(recorder: SqlRecorder, turn_index: int, tokens_before: int) -> None:
    await recorder.record_event(
        TASK,
        "compaction",
        {"turn_index": turn_index, "tokens_before": tokens_before, "tokens_after": 900},
    )


# ---------- a task that has a row ----------
async def test_a_task_with_a_row_is_read_from_the_row(
    scratch: Scratch, sessions: async_sessionmaker[AsyncSession]
) -> None:
    scratch.add_task_row(TASK, compactions=4, max_fraction=0.57)

    totals = await read_context_totals(sessions, TASK, WINDOW)

    assert totals == ContextTotals(compactions=4, max_fraction=0.57)


async def test_a_task_with_a_row_does_not_need_a_window(
    scratch: Scratch, sessions: async_sessionmaker[AsyncSession]
) -> None:
    scratch.add_task_row(TASK, compactions=0, max_fraction=0.0)

    assert await read_context_totals(sessions, TASK, None) == ContextTotals(0, 0.0)


# ---------- a task without one, as `longhaul run` leaves it ----------
async def test_a_task_without_a_row_is_read_from_its_log(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    recorder = SqlRecorder(sessions)
    await log_turn(recorder, 1, tokens=2_000)
    await log_turn(recorder, 2, tokens=4_800, cached=3_000)
    await log_turn(recorder, 3, tokens=1_500)
    await log_compaction(recorder, 2, 4_800)
    await log_compaction(recorder, 3, 4_700)

    totals = await read_context_totals(sessions, TASK, WINDOW)

    # The cache read counts: it is part of the prompt that took room in the window.
    assert totals == ContextTotals(compactions=2, max_fraction=pytest.approx(4_800 / WINDOW))


async def test_the_compactor_calls_are_not_the_fullest_request(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    recorder = SqlRecorder(sessions)
    await log_turn(recorder, 1, tokens=2_000)
    await log_turn(recorder, 2, role="compactor", tokens=7_900)

    totals = await read_context_totals(sessions, TASK, WINDOW)

    assert totals == ContextTotals(compactions=0, max_fraction=pytest.approx(2_000 / WINDOW))


async def test_one_compaction_that_was_logged_twice_counts_once(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    # An activity that ran again after its write went through leaves an identical line.
    recorder = SqlRecorder(sessions)
    await log_turn(recorder, 1, tokens=2_000)
    await log_compaction(recorder, 1, 2_000)
    await log_compaction(recorder, 1, 2_000)

    totals = await read_context_totals(sessions, TASK, WINDOW)

    assert totals is not None and totals.compactions == 1


async def test_another_task_is_not_counted(sessions: async_sessionmaker[AsyncSession]) -> None:
    recorder = SqlRecorder(sessions)
    await log_turn(recorder, 1, tokens=2_000)
    await recorder.record_event("lh-local-other-1a2b3c4d", "compaction", {"turn_index": 1})

    totals = await read_context_totals(sessions, TASK, WINDOW)

    assert totals is not None and totals.compactions == 0


async def test_without_a_window_the_share_is_not_known(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    recorder = SqlRecorder(sessions)
    await log_turn(recorder, 1, tokens=2_000)
    await log_compaction(recorder, 1, 2_000)

    assert await read_context_totals(sessions, TASK, None) == ContextTotals(1, None)


async def test_a_task_the_database_has_never_seen_has_no_totals(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    assert await read_context_totals(sessions, "lh-local-nothing-ffffffff", WINDOW) is None


# ---------- the command's own reader ----------
def test_the_command_reads_the_database_of_the_environment(
    scratch: Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch.add_task_row(TASK, compactions=5, max_fraction=0.59)
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", scratch.url)
    get_settings.cache_clear()

    totals = asyncio.run(cli.context_totals_of(TASK, WINDOW))

    assert totals == ContextTotals(compactions=5, max_fraction=0.59)
