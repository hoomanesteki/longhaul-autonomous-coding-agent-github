"""The per-task context totals in `tasks` (LH-406), against Postgres.

Five columns hold what Phase 9 reads for a task: the tokens it sent as fresh input, the tokens it
got back, the tokens served from the prompt cache, the compactions it ran, and the largest share
of the window one request took. The activities write them, never the workflow, and an activity
that runs again must not count its call twice. That is why none of them is an increment: the
token totals are the sum of the cost ledger rows, the compactions are the distinct `compaction`
events, and the share is a maximum.

Every test works on a throwaway database that the test creates and drops, like the other table
tests do. test_migrations.py already checks that the migrated schema equals the models, so these
tests add what it cannot see: the place of the migration on the path, the defaults, and what the
ledger and the recorder write.
"""

import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.contracts import Usage
from longhaul.cost.ledger import SqlLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import SqlRecorder
from longhaul.db.session import make_engine
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase4]

ROOT = Path(__file__).resolve().parents[2]
PRICING = ROOT / "config" / "pricing.yaml"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_totaltest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REVISION = "0010_task_context_totals"
SONNET = "claude-sonnet-5-5"
TASK = "lh-acme-calc-42"
COLUMNS = ("input_tokens_total", "output_tokens_total", "cache_read_total", "compactions")
_COLUMNS_OF_TASKS: LiteralString = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = 'public' AND table_name = 'tasks'"
)


@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def config(self) -> Config:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", self.url.replace("%", "%%"))
        return cfg

    def rows(self, statement: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement, params).fetchall()

    def add_task(self, task_id: str = TASK) -> None:
        with psycopg.connect(self.url) as conn:
            conn.execute(
                "INSERT INTO tasks (id, repo, title, status, phase) "
                "VALUES (%s, 'acme/calc', 'a task', 'active', 'queued')",
                (task_id,),
            )

    def totals(self, task_id: str = TASK) -> tuple[Any, ...]:
        (row,) = self.rows(
            "SELECT input_tokens_total, output_tokens_total, cache_read_total, compactions, "
            "max_fraction FROM tasks WHERE id = %s",
            task_id,
        )
        return row

    def task_count(self) -> int:
        ((count,),) = self.rows("SELECT count(*) FROM tasks")
        return int(count)


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
        yield Scratch(name, url)
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def database(scratch: Scratch) -> Scratch:
    command.upgrade(scratch.config(), "head")
    return scratch


@pytest.fixture
async def sessions(database: Scratch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(database.url)
    try:
        yield async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def ledger(sessions: async_sessionmaker[AsyncSession]) -> SqlLedger:
    return SqlLedger(sessions, Pricing.load(PRICING))


@pytest.fixture
def recorder(sessions: async_sessionmaker[AsyncSession]) -> SqlRecorder:
    return SqlRecorder(sessions)


async def call(ledger: SqlLedger, usage: Usage, *, task_id: str = TASK, attempt: int = 1) -> None:
    await ledger.record(task_id, "acme/calc", SONNET, 0, attempt, usage)


# ---------- the migration ----------
def test_the_columns_are_one_step_on_the_migration_path(scratch: Scratch) -> None:
    cfg = scratch.config()
    # The step below this one is read from the scripts, so that a migration chained in front
    # of it later does not break the test.
    revision = ScriptDirectory.from_config(cfg).get_revision(REVISION)
    assert revision is not None
    below = revision.down_revision
    assert isinstance(below, str)

    command.upgrade(cfg, "head")
    assert {name for (name,) in scratch.rows(_COLUMNS_OF_TASKS)} >= {*COLUMNS, "max_fraction"}

    command.downgrade(cfg, below)
    assert not {name for (name,) in scratch.rows(_COLUMNS_OF_TASKS)} & {*COLUMNS, "max_fraction"}

    command.upgrade(cfg, "head")
    assert "max_fraction" in {name for (name,) in scratch.rows(_COLUMNS_OF_TASKS)}


def test_a_row_that_existed_before_the_migration_starts_at_zero(scratch: Scratch) -> None:
    cfg = scratch.config()
    revision = ScriptDirectory.from_config(cfg).get_revision(REVISION)
    assert revision is not None and isinstance(revision.down_revision, str)
    command.upgrade(cfg, revision.down_revision)
    scratch.add_task()

    command.upgrade(cfg, REVISION)

    assert scratch.totals() == (0, 0, 0, 0, 0.0)


def test_a_new_task_row_starts_at_zero(database: Scratch) -> None:
    database.add_task()
    assert database.totals() == (0, 0, 0, 0, 0.0)


def test_the_token_totals_hold_more_than_two_billion(database: Scratch) -> None:
    # A long task on a big window re-sends its prompt every turn. The columns are 64 bit.
    database.add_task()
    with psycopg.connect(database.url) as conn:
        conn.execute("UPDATE tasks SET input_tokens_total = 5000000000 WHERE id = %s", (TASK,))
    assert database.totals()[0] == 5_000_000_000


# ---------- the tokens, from the ledger ----------
async def test_the_ledger_sets_the_token_totals_from_its_rows(
    database: Scratch, ledger: SqlLedger
) -> None:
    database.add_task()
    await call(ledger, Usage(input_tokens=1000, output_tokens=50, cache_read_tokens=200))
    await call(
        ledger,
        Usage(input_tokens=300, output_tokens=70, cache_read_tokens=900, cache_write_tokens=40),
    )

    fresh, output, cached, compactions, _ = database.totals()
    # A cache write is read for the first time, so it counts as fresh input: the three totals do
    # not overlap, and their sum is everything the model processed.
    assert (fresh, output, cached) == (1000 + 300 + 40, 50 + 70, 200 + 900)
    assert compactions == 0


async def test_the_ledger_counts_each_call_once_and_a_retry_as_the_call_it_is(
    database: Scratch, ledger: SqlLedger
) -> None:
    # A retried llm_turn calls the model again and gets a row of its own (attempt 2). Both calls
    # were billed, so both are in the total. Nothing is added twice for one call.
    database.add_task()
    usage = Usage(input_tokens=500, output_tokens=20)
    await call(ledger, usage, attempt=1)
    assert database.totals()[:2] == (500, 20)
    await call(ledger, usage, attempt=2)
    assert database.totals()[:2] == (1000, 40)


async def test_the_totals_of_one_task_do_not_include_another(
    database: Scratch, ledger: SqlLedger
) -> None:
    database.add_task()
    database.add_task("lh-acme-calc-43")
    await call(ledger, Usage(input_tokens=500, output_tokens=20))
    await call(ledger, Usage(input_tokens=700, output_tokens=30), task_id="lh-acme-calc-43")

    assert database.totals(TASK)[:2] == (500, 20)
    assert database.totals("lh-acme-calc-43")[:2] == (700, 30)


async def test_the_cost_total_still_follows_the_rows(database: Scratch, ledger: SqlLedger) -> None:
    database.add_task()
    usage = Usage(input_tokens=1_000_000)
    price = Pricing.load(PRICING).cost(usage, SONNET)
    await call(ledger, usage)
    await call(ledger, usage)

    ((cost,),) = database.rows("SELECT cost_usd FROM tasks WHERE id = %s", TASK)
    assert cost == (price * 2).quantize(Decimal("0.0001"))


async def test_a_task_without_a_row_is_not_an_error(database: Scratch, ledger: SqlLedger) -> None:
    # A local run has no row in `tasks`. Its calls are on the ledger, and nothing is created.
    await call(ledger, Usage(input_tokens=500, output_tokens=20))

    assert database.task_count() == 0
    ((rows,),) = database.rows("SELECT count(*) FROM cost_ledger")
    assert rows == 1


async def test_the_totals_catch_up_at_the_first_call_after_the_row_exists(
    database: Scratch, ledger: SqlLedger
) -> None:
    await call(ledger, Usage(input_tokens=500, output_tokens=20))
    database.add_task()
    assert database.totals()[:2] == (0, 0)

    await call(ledger, Usage(input_tokens=100, output_tokens=5))

    assert database.totals()[:2] == (600, 25)


# ---------- the share of the window ----------
async def test_the_share_only_goes_up(database: Scratch, recorder: SqlRecorder) -> None:
    database.add_task()

    await recorder.record_request_fraction(TASK, 0.30)
    assert database.totals()[4] == pytest.approx(0.30)
    await recorder.record_request_fraction(TASK, 0.20)  # a smaller request changes nothing
    assert database.totals()[4] == pytest.approx(0.30)
    await recorder.record_request_fraction(TASK, 0.55)
    assert database.totals()[4] == pytest.approx(0.55)
    await recorder.record_request_fraction(TASK, 0.55)  # and a run of the same activity again
    assert database.totals()[4] == pytest.approx(0.55)


async def test_a_share_of_a_task_without_a_row_is_dropped_quietly(
    database: Scratch, recorder: SqlRecorder
) -> None:
    await recorder.record_request_fraction(TASK, 0.30)

    assert database.task_count() == 0


async def test_the_share_of_one_task_is_not_the_share_of_another(
    database: Scratch, recorder: SqlRecorder
) -> None:
    database.add_task()
    database.add_task("lh-acme-calc-43")

    await recorder.record_request_fraction(TASK, 0.60)
    await recorder.record_request_fraction("lh-acme-calc-43", 0.10)

    assert database.totals(TASK)[4] == pytest.approx(0.60)
    assert database.totals("lh-acme-calc-43")[4] == pytest.approx(0.10)


# ---------- the compactions, from the events ----------
def compaction(turn: int, before: int = 5000, after: int = 2000) -> dict[str, Any]:
    return {
        "turn_index": turn,
        "forced": False,
        "status": "ok",
        "tokens_before": before,
        "tokens_after": after,
        "folded_turns": 4,
    }


async def test_each_compaction_event_adds_one_to_the_task(
    database: Scratch, recorder: SqlRecorder
) -> None:
    database.add_task()

    await recorder.record_event(TASK, "compaction", compaction(5))
    assert database.totals()[3] == 1
    await recorder.record_event(TASK, "compaction", compaction(11, before=4200, after=1800))
    assert database.totals()[3] == 2


async def test_the_same_compaction_recorded_again_counts_once(
    database: Scratch, recorder: SqlRecorder
) -> None:
    # A local activity that runs again after a kill writes the same event a second time. The log
    # shows two lines (an event is a log line), and the task has still compacted once.
    database.add_task()

    await recorder.record_event(TASK, "compaction", compaction(5))
    await recorder.record_event(TASK, "compaction", compaction(5))

    assert database.totals()[3] == 1
    ((lines,),) = database.rows(
        "SELECT count(*) FROM events WHERE task_id = %s AND type = 'compaction'", TASK
    )
    assert lines == 2


async def test_a_forced_and_a_proactive_compaction_of_one_turn_are_two(
    database: Scratch, recorder: SqlRecorder
) -> None:
    database.add_task()

    await recorder.record_event(TASK, "compaction", compaction(5, before=6900, after=2500))
    await recorder.record_event(
        TASK, "compaction", {**compaction(5, before=3000, after=1200), "forced": True}
    )

    assert database.totals()[3] == 2


async def test_other_events_do_not_count_as_compactions(
    database: Scratch, recorder: SqlRecorder
) -> None:
    database.add_task()

    await recorder.record_event(TASK, "continue_as_new", {"turn_index": 40})
    await recorder.record_event(TASK, "sandbox_recreated", {})

    assert database.totals()[3] == 0


async def test_compactions_of_one_task_are_not_those_of_another(
    database: Scratch, recorder: SqlRecorder
) -> None:
    database.add_task()
    database.add_task("lh-acme-calc-43")

    await recorder.record_event(TASK, "compaction", compaction(5))
    await recorder.record_event("lh-acme-calc-43", "compaction", compaction(5))
    await recorder.record_event("lh-acme-calc-43", "compaction", compaction(9, before=4100))

    assert database.totals(TASK)[3] == 1
    assert database.totals("lh-acme-calc-43")[3] == 2


async def test_a_compaction_event_of_a_task_without_a_row_is_still_logged(
    database: Scratch, recorder: SqlRecorder
) -> None:
    await recorder.record_event(TASK, "compaction", compaction(5))

    assert database.task_count() == 0
    ((lines,),) = database.rows("SELECT count(*) FROM events WHERE task_id = %s", TASK)
    assert lines == 1
