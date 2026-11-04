"""The cost ledger against Postgres and a Temporal server (LH-701, invariant I6).

Every provider call writes a `cost_ledger` row, also a call whose reply is thrown away, a call
that was billed and then failed, and a call whose activity failed after the reply came back.
The workflow tests run TaskWorkflow with the real llm_turn on a time skipping test server, so a
retry's backoff costs no real time. The provider is scripted, but the model id is a real one,
so the ledger prices the calls with the checked-in pricing file. The ledger writes to a
throwaway Postgres database that is migrated to head and dropped afterwards.

The other tests use the SQL ledger on its own: the task total, a task with no row, calls that
run together, and the proof that the memory ledger of the unit tests prices as it does.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
import yaml
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import WorkflowHandle, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Outcome,
    OutcomeKind,
    ProviderError,
    ProviderErrorKind,
    RepoSettings,
    Sandbox,
    TaskSpec,
    Usage,
)
from longhaul.cost.ledger import MemoryLedger, SqlLedger
from longhaul.cost.pricing import Pricing, PricingError
from longhaul.db.records import SqlRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import EnvSettings, get_settings
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
PRICING = ROOT / "config" / "pricing.yaml"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_ledgertest_"
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
BASE = "b" * 40
WAIT_S = 30.0
SONNET = "claude-sonnet-5-5"
MILLIONTH = Decimal("0.000001")

CFG = LoopConfig(model=SONNET, max_turns=10, max_tokens_per_turn=1000, max_tokens_retry=5000)


# ---------- the Temporal server and the database ----------
@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def rows(self, statement: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement, params).fetchall()

    def execute(self, statement: LiteralString, *params: Any) -> None:
        with psycopg.connect(self.url) as conn:
            conn.execute(statement, params)

    def add_task(self, task_id: str, repo: str = "acme/calc") -> None:
        self.execute(
            "INSERT INTO tasks (id, repo, title, status, phase) "
            "VALUES (%s, %s, 'a task', 'running', 'coding')",
            task_id,
            repo,
        )

    def task_total(self, task_id: str) -> Decimal:
        ((total,),) = self.rows("SELECT cost_usd FROM tasks WHERE id = %s", task_id)
        assert isinstance(total, Decimal)
        return total

    def ledger_rows(self) -> int:
        ((count,),) = self.rows("SELECT count(*) FROM cost_ledger")
        return int(count)


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
    """A fresh database at the head revision. Alembic must not reach the application database."""
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
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield Scratch(name, url)
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


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


# ---------- a worker with the real activities ----------
class Boxes:
    """prepare_workspace and cleanup for the tests, with one fake sandbox per task."""

    def __init__(self) -> None:
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        self.boxes[box.id] = box
        return acts.PrepareWorkspaceOutput(sandbox_id=box.id, base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        return None

    async def destroy_all(self) -> None:
        for box in self.boxes.values():
            await box.destroy()


class FlakyBlobs(MemoryBlobStore):
    """A blob store whose first write fails, as a lost connection would."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def put(self, data: bytes, *, key: str) -> BlobRef:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("the blob store is unreachable")
        return await super().put(data, key=key)


class BilledThenFailed(ScriptedProvider):
    """The first call is billed and then fails, as a reply whose body does not parse is."""

    BILLED = Usage(input_tokens=700, output_tokens=30)

    def __init__(self, script: Path) -> None:
        super().__init__(script)
        self.calls = 0

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError(ProviderErrorKind.SERVER, "body did not parse", usage=self.BILLED)
        return await super().complete(**kwargs)


@asynccontextmanager
async def running(
    env: WorkflowEnvironment,
    *,
    provider: ScriptedProvider,
    sessions: async_sessionmaker[AsyncSession],
    ledger: SqlLedger,
    blobs: MemoryBlobStore | None = None,
) -> AsyncIterator[str]:
    """A worker with the real llm_turn, tool_exec and append_results. Yields its task queue."""
    store = blobs or MemoryBlobStore()
    recorder = SqlRecorder(sessions)
    registry = default_registry(blobs=store)
    boxes = Boxes()
    llm = LlmActivities(
        provider, store, recorder, registry.specs(), lambda role: CFG, ledger=ledger
    )
    tools = ToolActivities(registry, boxes, store, recorder, checkpoints=NoCheckpoints())
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[*llm.activities, *tools.activities, boxes.prepare_workspace, boxes.cleanup],
            workflow_runner=workflow_runner(),
        ):
            yield queue
    finally:
        await boxes.destroy_all()


def make_spec() -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}",
        repo="acme/calc",
        issue_number=42,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(repo="acme/calc", budgets=Budgets(task_turns=CFG.max_turns)),
    )


def write_script(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    path = tmp_path / "script.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}), encoding="utf-8")
    return path


FINISH_AT_ONCE = [
    {
        "match": {"turn_index": 0},
        "respond": {"tool_calls": [{"name": "finish", "input": {"summary": "Nothing to do."}}]},
    }
]

# Three calls: a tool turn, a reply that pauses (it is thrown away and its text is sent back),
# and the call that continues it. The second and the third belong to the same turn.
THREE_CALLS = [
    {
        "match": {"turn_index": 0},
        "respond": {
            "text": "Looking around.",
            "tool_calls": [{"name": "list_dir", "input": {"path": "."}}],
        },
    },
    {
        "match": {"turn_index": 1},
        "respond": {"text": "Thinking it over.", "stop_reason": "pause_turn"},
    },
    {
        "match": {"turn_index": 2, "request_contains": "Thinking it over."},
        "respond": {"tool_calls": [{"name": "finish", "input": {"summary": "Left it alone."}}]},
    },
]


async def result_of(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    return await asyncio.wait_for(handle.result(), WAIT_S)


def scheduled(history: WorkflowHistory) -> list[str]:
    """The names of the activities the workflow scheduled, in order."""
    return [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]


def started_attempts(history: WorkflowHistory, activity_name: str) -> list[int]:
    """The attempt each activity of this type was on when the history recorded its start."""
    ids = {
        event.event_id
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name == activity_name
    }
    return [
        event.activity_task_started_event_attributes.attempt
        for event in history.events
        if event.HasField("activity_task_started_event_attributes")
        and event.activity_task_started_event_attributes.scheduled_event_id in ids
    ]


def priced(usage: Usage) -> Decimal:
    return Pricing.load(PRICING).cost(usage, SONNET)


LEDGER_ROW = (
    "SELECT task_id, repo, model, turn_index, attempt, input_tokens, output_tokens, "
    "cache_read_tokens, cache_write_tokens, usd FROM cost_ledger ORDER BY id"
)


# ---------- invariant I6 ----------
async def test_every_call_ledgered(
    env: WorkflowEnvironment,
    database: Scratch,
    sessions: async_sessionmaker[AsyncSession],
    ledger: SqlLedger,
    tmp_path: Path,
) -> None:
    provider = ScriptedProvider(write_script(tmp_path, THREE_CALLS))
    spec = make_spec()
    database.add_task(spec.task_id)
    async with running(env, provider=provider, sessions=sessions, ledger=ledger) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Left it alone.")
    calls = len(provider.options_seen)
    assert calls == 3

    # One row for each call, with the task, the repo, the model and the usage the provider
    # reported. The paused reply was not stored, and its call has a row all the same. The turn
    # and the attempt do not tell the second and the third call apart: both belong to turn 1,
    # and each ran as the first attempt of its own activity.
    rows = database.rows(LEDGER_ROW)
    assert len(rows) == calls
    assert [(r[0], r[1], r[2]) for r in rows] == [(spec.task_id, "acme/calc", SONNET)] * calls
    assert [(r[3], r[4]) for r in rows] == [(0, 1), (1, 1), (1, 1)]
    for *_, input_tokens, output_tokens, cache_read, cache_write, usd in rows:
        usage = Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        )
        assert input_tokens > 0 and output_tokens > 0
        assert abs(usd - priced(usage)) <= MILLIONTH / 2

    # The cost the workflow reports, the turn rows and the ledger add up to the same money.
    spent = sum((r[9] for r in rows), Decimal(0))
    assert status.cost_usd == pytest.approx(float(spent))
    ((turn_total,),) = database.rows("SELECT sum(cost_usd) FROM turns")
    assert turn_total == spent
    # The task row existed before the first call. Its total is the sum, to ten thousandths.
    assert database.task_total(spec.task_id) == spent.quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )


async def test_retry_ledgered_twice(
    env: WorkflowEnvironment,
    database: Scratch,
    sessions: async_sessionmaker[AsyncSession],
    ledger: SqlLedger,
    tmp_path: Path,
) -> None:
    """The reply comes back, then the blob store fails, so the activity fails after the call.

    Temporal runs llm_turn again and the model is called again. Both calls were billed, so the
    ledger has two rows, with attempts 1 and 2. The turns table has one row, for the reply that
    was used.
    """
    provider = ScriptedProvider(write_script(tmp_path, FINISH_AT_ONCE))
    async with running(
        env, provider=provider, sessions=sessions, ledger=ledger, blobs=FlakyBlobs()
    ) as queue:
        handle = await start_task(env.client, make_spec(), task_queue=queue)
        outcome = await result_of(handle)
        history = await handle.fetch_history()

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Nothing to do.")
    # One activity, run twice: the history shows the attempt it finished on.
    assert scheduled(history).count("llm_turn") == 1
    assert started_attempts(history, "llm_turn") == [2]
    assert len(provider.options_seen) == 2

    rows = database.rows("SELECT turn_index, attempt, usd FROM cost_ledger ORDER BY id")
    assert [(turn, attempt) for turn, attempt, _ in rows] == [(0, 1), (0, 2)]
    ((turn_attempt, turn_cost),) = database.rows("SELECT attempt, cost_usd FROM turns")
    assert turn_attempt == 1
    assert sum(usd for _, _, usd in rows) == 2 * turn_cost


async def test_a_billed_call_that_failed_is_ledgered_and_so_is_its_retry(
    env: WorkflowEnvironment,
    database: Scratch,
    sessions: async_sessionmaker[AsyncSession],
    ledger: SqlLedger,
    tmp_path: Path,
) -> None:
    provider = BilledThenFailed(write_script(tmp_path, FINISH_AT_ONCE))
    async with running(env, provider=provider, sessions=sessions, ledger=ledger) as queue:
        handle = await start_task(env.client, make_spec(), task_queue=queue)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert provider.calls == 2
    rows = database.rows(
        "SELECT attempt, input_tokens, output_tokens, usd FROM cost_ledger ORDER BY id"
    )
    # The failed call has the usage the provider reported before it failed: 700 * 2.00 +
    # 30 * 10.00 = 1700 per million tokens, so 0.0017 dollars.
    assert rows[0] == (1, 700, 30, Decimal("0.0017"))
    assert [row[0] for row in rows] == [1, 2]


async def test_a_call_that_was_not_billed_has_no_row(
    env: WorkflowEnvironment,
    database: Scratch,
    sessions: async_sessionmaker[AsyncSession],
    ledger: SqlLedger,
) -> None:
    # Two overloaded answers, then a reply: the provider refused the first two before billing.
    provider = ScriptedProvider(
        ROOT / "tests" / "fixtures" / "scripts" / "activities_overloaded.yaml"
    )
    async with running(env, provider=provider, sessions=sessions, ledger=ledger) as queue:
        handle = await start_task(env.client, make_spec(), task_queue=queue)
        await result_of(handle)

    assert len(provider.options_seen) == 3
    assert database.ledger_rows() == 1


# ---------- the SQL ledger on its own ----------
async def test_a_row_holds_the_call_and_its_price(database: Scratch, ledger: SqlLedger) -> None:
    usage = Usage(input_tokens=336, output_tokens=482, cache_write_tokens=6504)

    cost = await ledger.record("lh-acme-calc-42", "acme/calc", SONNET, 3, 2, usage)

    assert cost == Decimal("0.021752")
    assert database.rows(LEDGER_ROW) == [
        ("lh-acme-calc-42", "acme/calc", SONNET, 3, 2, 336, 482, 0, 6504, Decimal("0.021752"))
    ]


async def test_the_task_total_is_the_sum_of_its_rows(database: Scratch, ledger: SqlLedger) -> None:
    database.add_task("lh-x")
    # 70 input tokens at 2.00 dollars per million tokens cost 0.00014. The column of the task
    # holds ten thousandths, so adding each call on its own would round every one of them to
    # 0.0001 and stop at 0.0003. The total is the sum of the rows, rounded once: 0.00042.
    for attempt in (1, 2, 3):
        await ledger.record("lh-x", "acme/calc", SONNET, 0, attempt, Usage(input_tokens=70))

    assert database.task_total("lh-x") == Decimal("0.0004")
    ((exact,),) = database.rows("SELECT sum(usd) FROM cost_ledger WHERE task_id = 'lh-x'")
    assert exact == Decimal("0.000420")


async def test_every_task_keeps_its_own_total(database: Scratch, ledger: SqlLedger) -> None:
    database.add_task("lh-a")
    database.add_task("lh-b")
    await ledger.record("lh-a", "acme/calc", SONNET, 0, 1, Usage(input_tokens=1_000))
    await ledger.record("lh-b", "acme/other", SONNET, 0, 1, Usage(input_tokens=3_000))
    await ledger.record("lh-a", "acme/calc", SONNET, 1, 1, Usage(input_tokens=1_000))

    assert (database.task_total("lh-a"), database.task_total("lh-b")) == (
        Decimal("0.0040"),
        Decimal("0.0060"),
    )


async def test_a_task_that_has_no_row_yet_is_not_an_error(
    database: Scratch, ledger: SqlLedger
) -> None:
    # A local run has a task id and no row in tasks.
    cost = await ledger.record("lh-local", "local:/calc", SONNET, 0, 1, Usage(input_tokens=1_000))

    assert cost == Decimal("0.002")
    assert database.ledger_rows() == 1
    assert database.rows("SELECT count(*) FROM tasks") == [(0,)]


async def test_calls_made_before_the_task_row_existed_are_counted_once_it_does(
    database: Scratch, ledger: SqlLedger
) -> None:
    for turn in (0, 1):
        await ledger.record("lh-x", "acme/calc", SONNET, turn, 1, Usage(input_tokens=1_000))
    database.add_task("lh-x")
    assert database.task_total("lh-x") == 0

    await ledger.record("lh-x", "acme/calc", SONNET, 2, 1, Usage(input_tokens=1_000))

    assert database.task_total("lh-x") == Decimal("0.0060")


async def test_calls_that_are_recorded_together_are_all_counted(
    database: Scratch, ledger: SqlLedger
) -> None:
    database.add_task("lh-x")
    calls = [
        ledger.record("lh-x", "acme/calc", SONNET, turn, 1, Usage(input_tokens=70))
        for turn in range(25)
    ]
    await asyncio.gather(*calls)

    # 25 * 0.00014 = 0.0035. Each record adds its row and reads the sum, and a record that did not
    # wait for the one before it would leave that one out of its total.
    assert database.ledger_rows() == 25
    assert database.task_total("lh-x") == Decimal("0.0035")


async def test_a_model_without_a_price_writes_nothing(database: Scratch, ledger: SqlLedger) -> None:
    database.add_task("lh-x")
    with pytest.raises(PricingError, match="claude-opus-9"):
        await ledger.record("lh-x", "acme/calc", "claude-opus-9", 0, 1, Usage(input_tokens=5))

    assert database.ledger_rows() == 0
    assert database.task_total("lh-x") == 0


@pytest.mark.parametrize(
    "usage",
    [
        Usage(input_tokens=336, output_tokens=482, cache_write_tokens=6504),
        Usage(input_tokens=5457, output_tokens=1388, cache_read_tokens=6504),
        Usage(input_tokens=1, cache_write_tokens=3),
        Usage(),
    ],
)
async def test_the_memory_ledger_prices_and_rounds_like_the_sql_ledger(
    ledger: SqlLedger, usage: Usage
) -> None:
    memory = MemoryLedger(Pricing.load(PRICING))
    for model in (SONNET, "claude-haiku-5-5"):
        assert await ledger.record("lh-x", "r", model, 0, 1, usage) == await memory.record(
            "lh-x", "r", model, 0, 1, usage
        )
