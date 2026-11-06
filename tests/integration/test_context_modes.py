"""The context modes and the per-task totals (LH-406), with the real activities.

`context.mode` says which of the two context tiers a run uses: `full` (elide old tool outputs
and compact), `no_compaction` (elide only) or `none` (neither; the hard limit still ends the run).
The `sliding` mode of the naive baseline (LH-903) has neither tier and cuts the oldest turns at the
hard limit instead. A run can name its own mode, and that wins over the config of the worker.

The workflow runs on a time skipping Temporal test server with the real llm_turn, compact,
tool_exec, append_results and record_event activities, a fake sandbox for the commands, the
memory blob store, and a throwaway Postgres database that is migrated to head and dropped
afterwards. The ledger and the recorder write to that database, so the `tasks` row is read back
the way a report would read it. A task has a row only when something made one, so each test
inserts the row first, as the poller does for a task it starts.

Two scripted tasks serve the tests. The calc task is short and never comes near the window. The
long task is the one of test_compaction.py: sixty turns that each print a block of text, in a
window of 8,000 tokens.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
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
from temporalio.client import WorkflowExecutionStatus
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.fakes.scripted_runs import SCRIPTS, TEST_COMMAND, seed

from longhaul.agent.loop import LoopConfig
from longhaul.agent.turns import validate_pairs
from longhaul.blobs.base import BlobStoreBase
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.context.compact import NOTES_TOOL_NAME
from longhaul.context.evict import ELIDED_PREFIX
from longhaul.context.log import read_log
from longhaul.context.modes import ContextMode
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    Phase,
    ProgressNotes,
    RepoSettings,
    Role,
    Sandbox,
    TaskSpec,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from longhaul.cost.ledger import SqlLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import SqlRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.context import ContextActivities
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.client import get_task, start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.base import turn_index
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import Context, EnvSettings, get_settings
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase4]

ROOT = Path(__file__).resolve().parents[2]
PRICING = ROOT / "config" / "pricing.yaml"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_modetest_"
# If alembic ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
BASE = "b" * 40
WAIT_S = 180.0
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

WINDOW = 8_000
POLICY = Context(
    window_tokens=WINDOW,
    compact_at=0.6,
    hard_limit=0.85,
    keep_recent_turns=4,
    elide_tool_outputs_after_turns=2,
    tool_output_max_tokens=250,
)
LONG_TURNS = 60
NOTES = ProgressNotes(
    goal="Fix every module of the calc package",
    plan_status=[],
    files_touched=["calc/add.py"],
    decisions=["Fix one module at a time and run its tests"],
    failing_tests=[],
    open_hypotheses=[],
    tried_and_failed=[],
    covers_turns_up_to=0,
)
SAID = (
    "I read the module, found the line that is wrong and changed it. The tests for this module "
    "now pass, so I move on to the next one. "
)
CONTEXT_LIMIT = Outcome(kind=OutcomeKind.ERROR, summary="context limit")
FINISHED = Outcome(kind=OutcomeKind.PR_OPENED, summary="Fixed them all.")
CALC_FINISHED = Outcome(
    kind=OutcomeKind.PR_OPENED, summary="Changed add() to return a + b. All tests pass."
)


def config_for(role: Role) -> LoopConfig:
    return LoopConfig(
        model=HAIKU if role is Role.COMPACTOR else SONNET,
        max_turns=200,
        max_tokens_per_turn=1000,
        max_tokens_retry=5000,
    )


# ---------- the long task ----------
def coder_turn(turn: int) -> dict[str, Any]:
    command_line = f"printf 'turn {turn} line %s: output of the test run\\n' $(seq 1 14)"
    return {
        "match": {"role": "coder", "turn_index": turn},
        "respond": {
            "text": f"Turn {turn}. {SAID * 4}",
            "tool_calls": [{"name": "run_command", "input": {"command": command_line}}],
        },
    }


def finish_turn(turn: int) -> dict[str, Any]:
    return {
        "match": {"role": "coder", "turn_index": turn},
        "respond": {"tool_calls": [{"name": "finish", "input": {"summary": "Fixed them all."}}]},
    }


def compactor_turn() -> dict[str, Any]:
    call = {"name": NOTES_TOOL_NAME, "input": NOTES.model_dump(mode="json")}
    return {"match": {"role": "compactor"}, "respond": {"tool_calls": [call]}}


def long_task(turns: int = LONG_TURNS) -> list[dict[str, Any]]:
    return [*(coder_turn(t) for t in range(turns - 1)), finish_turn(turns - 1), compactor_turn()]


def write_script(tmp_path: Path, steps: Sequence[dict[str, Any]]) -> Path:
    path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump({"steps": list(steps)}))
    return path


# ---------- the database ----------
@dataclass(frozen=True)
class Totals:
    """The columns that LH-406 adds to `tasks`, as one row of them."""

    input_tokens_total: int
    output_tokens_total: int
    cache_read_total: int
    compactions: int
    max_fraction: float


@dataclass(frozen=True)
class Scratch:
    name: str
    url: str

    def rows(self, statement: LiteralString, *params: Any) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement, params).fetchall()

    def add_task(self, task_id: str) -> None:
        with psycopg.connect(self.url) as conn:
            conn.execute(
                "INSERT INTO tasks (id, repo, title, status, phase) "
                "VALUES (%s, 'acme/calc', 'a task', 'active', 'queued')",
                (task_id,),
            )

    def totals(self, task_id: str) -> Totals:
        (row,) = self.rows(
            "SELECT input_tokens_total, output_tokens_total, cache_read_total, "
            "compactions, max_fraction FROM tasks WHERE id = %s",
            task_id,
        )
        return Totals(*row)

    def ledger_totals(self, task_id: str) -> tuple[int, int, int]:
        """Fresh input (input and cache writes), output and cache reads, from the ledger rows."""
        ((fresh, output, cached),) = self.rows(
            "SELECT coalesce(sum(input_tokens + cache_write_tokens), 0), "
            "coalesce(sum(output_tokens), 0), coalesce(sum(cache_read_tokens), 0) "
            "FROM cost_ledger WHERE task_id = %s",
            task_id,
        )
        return int(fresh), int(output), int(cached)

    def event_count(self, task_id: str, event_type: str) -> int:
        ((count,),) = self.rows(
            "SELECT count(*) FROM events WHERE task_id = %s AND type = %s", task_id, event_type
        )
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
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


# ---------- the worker ----------
class CachingProvider(ScriptedProvider):
    """A scripted provider that reports a share of each prompt as cache reads and cache writes.

    The prompt keeps its size: the share moves from `input_tokens` to the cache fields, so the
    token meter and the projections see what they would with a provider that caches. `usages`
    keeps what each call reported, by model, and is what the totals of the task are checked
    against.
    """

    def __init__(self, script: Path) -> None:
        super().__init__(script)
        self.usages: list[tuple[str, Usage]] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        resp = await super().complete(**kwargs)
        prompt = resp.usage.input_tokens
        read, write = prompt // 4, prompt // 10
        usage = Usage(
            input_tokens=prompt - read - write,
            output_tokens=resp.usage.output_tokens,
            cache_read_tokens=read,
            cache_write_tokens=write,
        )
        self.usages.append((kwargs["model"], usage))
        return resp.model_copy(update={"usage": usage})

    def of(self, model: str) -> list[Usage]:
        return [usage for name, usage in self.usages if name == model]


class SpyLlm(LlmActivities):
    """llm_turn that keeps its outputs, and counts the refusals before the call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.outputs: list[acts.LlmTurnOutput] = []
        self.refused = 0

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        try:
            out = await super().llm_turn(inp)
        except ApplicationError as exc:
            if exc.type == "context_too_long":
                self.refused += 1
            raise
        self.outputs.append(out)
        return out


class SpyTools(ToolActivities):
    """append_results that keeps the view each turn ended with."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.views: list[BlobRef] = []

    @activity.defn(name=acts.APPEND_RESULTS.name)
    async def append_results(self, inp: acts.AppendResultsInput) -> BlobRef:
        ref = await super().append_results(inp)
        self.views.append(ref)
        return ref


class Boxes:
    """prepare_workspace and the like for the tests, with one fake sandbox per task."""

    def __init__(self, repo: str | None) -> None:
        self.repo = repo
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        self.boxes[box.id] = box
        if self.repo is not None:
            await seed(box, self.repo)
        return acts.PrepareWorkspaceOutput(sandbox_id=box.id, base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        return None

    # A run of 40 turns hands over to a new run, which ensures its sandbox first.
    @activity.defn(name=acts.ENSURE_SANDBOX.name)
    async def ensure_sandbox(self, inp: acts.EnsureSandboxInput) -> str:
        return inp.sandbox_id or f"sbx-{inp.task_id}"

    @activity.defn(name=acts.BUNDLE_CHECKPOINT.name)
    async def bundle_checkpoint(self, inp: acts.BundleCheckpointInput) -> BlobRef:
        key = f"tasks/{inp.task_id}/ckpt/{inp.turn_index}.bundle"
        return BlobRef(key=key, sha256="0" * 64, size=1)

    async def destroy_all(self) -> None:
        for box in self.boxes.values():
            await box.destroy()


@dataclass
class Rig:
    """What a test looks at once the worker is up: the stores and the spies."""

    queue: str
    provider: CachingProvider
    llm: SpyLlm
    tools: SpyTools
    blobs: BlobStoreBase

    async def view(self) -> list[Message]:
        return await self.blobs.get_json(self.tools.views[-1], list[Message])


@asynccontextmanager
async def running(
    env: WorkflowEnvironment,
    database: Scratch,
    script: Path,
    *,
    mode: ContextMode,
    repo: str | None = None,
) -> AsyncIterator[Rig]:
    """A worker with the real activities, whose config says `mode`."""
    policy = POLICY.model_copy(update={"mode": mode})
    engine = make_engine(database.url)
    sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    blobs = MemoryBlobStore()
    provider = CachingProvider(script)
    recorder = SqlRecorder(sessions)
    ledger = SqlLedger(sessions, Pricing.load(PRICING))
    registry = default_registry(test_command=TEST_COMMAND, blobs=blobs)
    boxes = Boxes(repo)
    llm = SpyLlm(
        provider, blobs, recorder, registry.specs(), config_for, ledger=ledger, context=policy
    )
    compactor = ContextActivities(
        provider, blobs, registry.specs(), config_for, policy, ledger=ledger
    )
    tools = SpyTools(
        registry,
        boxes,
        blobs,
        recorder,
        checkpoints=NoCheckpoints(),
        tool_output_max_tokens=policy.tool_output_max_tokens,
    )
    events = EventActivities(recorder)
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *compactor.activities,
                *tools.activities,
                *events.activities,
                boxes.prepare_workspace,
                boxes.cleanup,
                boxes.ensure_sandbox,
                boxes.bundle_checkpoint,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield Rig(queue, provider, llm, tools, blobs)
    finally:
        await boxes.destroy_all()
        await engine.dispose()


def make_spec(title: str = "Every module of calc is wrong") -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}",
        repo="acme/calc",
        # No issue number: the task of an issue on an owner/name repo is delivered as a pull
        # request (LH-306), and this test runs the loop on a faked workspace with no GitHub.
        issue_number=None,
        title=title,
        body="Fix each module of the calc package and run its tests.",
        settings=RepoSettings(repo="acme/calc", budgets=Budgets(task_turns=200)),
    )


@dataclass
class Played:
    spec: TaskSpec
    worker: Rig
    outcome: Outcome
    phase: Phase
    totals: Totals
    view: list[Message]

    @property
    def turns(self) -> int:
        return len(self.worker.llm.outputs)


async def play(
    env: WorkflowEnvironment,
    database: Scratch,
    script: Path,
    *,
    mode: ContextMode,
    run_mode: ContextMode | None = None,
    repo: str | None = None,
) -> Played:
    """Run a task to its end on a worker whose config says `mode`.

    `run_mode` is the mode the run asks for, as `longhaul run --context-mode` does.
    """
    spec = make_spec()
    database.add_task(spec.task_id)
    async with running(env, database, script, mode=mode, repo=repo) as worker:
        handle = await start_task(env.client, spec, task_queue=worker.queue, context_mode=run_mode)
        outcome = await asyncio.wait_for(handle.result(), WAIT_S)
        # A task that ended, whatever it ended with, has a workflow that completed.
        described = await handle.describe()
        assert described.status is WorkflowExecutionStatus.COMPLETED
        status = await get_task(env.client, spec.task_id).query(TaskWorkflow.status)
        assert status.outcome == outcome
        return Played(
            spec, worker, outcome, status.phase, database.totals(spec.task_id), await worker.view()
        )


def text_of(messages: Sequence[Message]) -> str:
    """The text of the messages and the results in them, in one string."""
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolResultBlock):
                parts.append(block.content)
            elif isinstance(block, ToolUseBlock):
                parts.append(str(block.input))
    return "\n".join(parts)


# ---------- LH-406 acceptance criteria ----------
@pytest.mark.parametrize("mode", list(ContextMode), ids=lambda m: m.value)
async def test_modes_run(env: WorkflowEnvironment, database: Scratch, mode: ContextMode) -> None:
    """Each mode runs the calc task to its end, and the totals of the task are written.

    The totals are checked against what the provider reported for each of its calls, and against
    the ledger rows, which are the other record of the same calls. The window is far larger than
    the task, so no mode compacts here. What differs is tier 1: the older results of the view
    are stubs in `full` and `no_compaction` and are whole in `none` and `sliding`.
    """
    script = SCRIPTS / "calc_fix.yaml"
    played = await play(env, database, script, mode=mode, repo="calc")
    spec, worker = played.spec, played.worker

    assert played.outcome == CALC_FINISHED
    assert played.phase is Phase.DONE
    assert played.turns == 5

    usages = worker.provider.of(SONNET)
    assert len(usages) == 5
    fresh = sum(u.input_tokens + u.cache_write_tokens for u in usages)
    output = sum(u.output_tokens for u in usages)
    cached = sum(u.cache_read_tokens for u in usages)
    assert fresh > 0 and output > 0 and cached > 0

    totals = played.totals
    assert (totals.input_tokens_total, totals.output_tokens_total) == (fresh, output)
    assert totals.cache_read_total == cached
    assert database.ledger_totals(spec.task_id) == (fresh, output, cached)
    assert totals.compactions == 0
    assert worker.provider.of(HAIKU) == []

    # The biggest request of the task, as a share of the window. It is the projection that
    # llm_turn made before it sent the request, so it is not zero in any mode.
    biggest = max(out.request_tokens for out in worker.llm.outputs)
    assert biggest > 0
    assert totals.max_fraction == pytest.approx(biggest / WINDOW)
    assert 0 < totals.max_fraction < POLICY.compact_at

    stubs = ELIDED_PREFIX in text_of(played.view)
    assert stubs is mode.elides


async def test_none_mode_hits_limit_cleanly(
    env: WorkflowEnvironment, database: Scratch, tmp_path: Path
) -> None:
    """With no tier at all, the long task ends at the hard limit with a clear outcome.

    The task is not a crash: the workflow completes, with the outcome `error` and the summary
    "context limit", and nothing in it tried to compact.
    """
    script = write_script(tmp_path, long_task())
    played = await play(env, database, script, mode=ContextMode.NONE)
    worker = played.worker

    assert played.outcome == CONTEXT_LIMIT
    assert played.phase is Phase.FAILED
    # The guard in front of the call refused the request once. The workflow did not ask again.
    assert worker.llm.refused == 1
    assert 5 < played.turns < LONG_TURNS

    # Nothing was elided and nothing was compacted.
    assert ELIDED_PREFIX not in text_of(played.view)
    assert database.event_count(played.spec.task_id, "compaction") == 0
    assert worker.provider.of(HAIKU) == []

    # The totals are written all the same, and the last request sent was near the limit.
    totals = played.totals
    assert totals.compactions == 0
    assert POLICY.hard_limit - 0.1 < totals.max_fraction < POLICY.hard_limit
    fresh = sum(u.input_tokens + u.cache_write_tokens for u in worker.provider.of(SONNET))
    assert totals.input_tokens_total == fresh > 0


async def test_sliding_mode_runs_the_long_task_to_its_end_under_the_limit(
    env: WorkflowEnvironment, database: Scratch, tmp_path: Path
) -> None:
    """The naive baseline: no tier at all, and the oldest turns go when the window is full.

    The long task ends in `context limit` in mode `none`. In mode `sliding` the same task goes
    on, because each request that would pass the hard limit is cut to the newest turns first. No
    request is refused, none is projected over the limit, nothing is elided or compacted, and the
    view never holds a result without its call.
    """
    script = write_script(tmp_path, long_task())
    played = await play(env, database, script, mode=ContextMode.SLIDING)
    worker = played.worker

    assert played.outcome == FINISHED
    assert played.phase is Phase.DONE
    assert played.turns == LONG_TURNS
    assert worker.llm.refused == 0

    # Every request that went out was under the hard limit, as the guard projects it.
    limit = POLICY.hard_limit * WINDOW
    assert max(out.request_tokens for out in worker.llm.outputs) < limit
    assert played.totals.max_fraction < POLICY.hard_limit

    # The window slid and nothing else ran.
    assert played.totals.compactions == 0
    assert database.event_count(played.spec.task_id, "compaction") == 0
    assert worker.provider.of(HAIKU) == []
    assert ELIDED_PREFIX not in text_of(played.view)

    # The view holds fewer turns than the task took, pairs intact, and still counts the turns.
    validate_pairs(played.view)
    kept = sum(1 for m in played.view if m.role == "assistant")
    assert kept < LONG_TURNS
    assert turn_index(played.view) == LONG_TURNS
    # The log is the whole record: it has every turn that the window dropped from the view.
    logged = await read_log(worker.blobs, played.spec.task_id)
    assert sum(1 for m in logged if m.role == "assistant") == LONG_TURNS


async def test_the_modes_end_where_their_tiers_end(
    env: WorkflowEnvironment, database: Scratch, tmp_path: Path
) -> None:
    """The same long task in each mode. Every tier that is on lets the task go further.

    `none` stops first. `no_compaction` elides, so its requests grow more slowly and it gets
    further, and still stops at the limit, with no compaction. `full` finishes the task.
    """
    script = write_script(tmp_path, long_task())
    none = await play(env, database, script, mode=ContextMode.NONE)
    elide_only = await play(env, database, script, mode=ContextMode.NO_COMPACTION)
    full = await play(env, database, script, mode=ContextMode.FULL)

    assert none.outcome == elide_only.outcome == CONTEXT_LIMIT
    assert full.outcome == FINISHED
    assert none.turns < elide_only.turns < full.turns == LONG_TURNS

    assert none.totals.compactions == elide_only.totals.compactions == 0
    assert ELIDED_PREFIX in text_of(elide_only.view)
    assert elide_only.worker.provider.of(HAIKU) == []

    # `full` compacted several times, and every compaction is a row of the task and an event.
    assert full.totals.compactions >= 3
    assert database.event_count(full.spec.task_id, "compaction") == full.totals.compactions
    assert len(full.worker.provider.of(HAIKU)) >= full.totals.compactions
    assert full.totals.max_fraction < POLICY.hard_limit


@pytest.mark.parametrize(
    ("config", "asked", "finishes"),
    [
        pytest.param(ContextMode.NONE, ContextMode.FULL, True, id="config-none-run-full"),
        pytest.param(ContextMode.FULL, ContextMode.NONE, False, id="config-full-run-none"),
        pytest.param(
            ContextMode.FULL, ContextMode.NO_COMPACTION, False, id="config-full-run-no-compaction"
        ),
    ],
)
async def test_a_run_can_override_the_config_mode(
    env: WorkflowEnvironment,
    database: Scratch,
    tmp_path: Path,
    config: ContextMode,
    asked: ContextMode,
    finishes: bool,
) -> None:
    """The mode a run asks for travels with the task and wins over the config of the worker.

    It reaches llm_turn, which elides, and the workflow, which calls compact.
    """
    script = write_script(tmp_path, long_task())
    played = await play(env, database, script, mode=config, run_mode=asked)

    assert played.outcome == (FINISHED if finishes else CONTEXT_LIMIT)
    assert (played.totals.compactions > 0) is finishes
    assert (ELIDED_PREFIX in text_of(played.view)) is (asked is not ContextMode.NONE)
    if not finishes:
        assert played.worker.provider.of(HAIKU) == []
