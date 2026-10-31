"""LH-204 end to end: TaskWorkflow with the real llm_turn, tool_exec and append_results.

The provider is scripted. The sandbox is Docker in the calc test and a fake one in the others.
The recorder writes to a throwaway Postgres database (migrated to head, then dropped), and the
blobs go to MemoryBlobStore, or to MinIO when LONGHAUL_BLOB_ENDPOINT is set. A time skipping
Temporal test server runs the workflow, so a retry's backoff costs no real time.

prepare_workspace and cleanup are test stand-ins here: the real ones belong to LH-206 and
LH-208. The worker runs under the same sandbox settings as a production worker.
"""

import asyncio
import shlex
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
import yaml
from alembic import command
from alembic.config import Config
from google.protobuf.message import Message as ProtoMessage
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError, RetryState
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.agent.turns import validate_pairs
from longhaul.blobs.base import BlobStoreBase
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.blobs.s3 import S3BlobStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    Phase,
    RepoSettings,
    Sandbox,
    SandboxLimits,
    TaskSpec,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.db.records import SqlRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.support import heartbeating
from longhaul.durable.activities.tools import SandboxLookup, ToolActivities
from longhaul.durable.client import start_task
from longhaul.durable.payload_guard import DEFAULT_LIMIT
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import EnvSettings, get_settings
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SCRIPTS = FIXTURES / "scripts"
CALC = FIXTURES / "repos" / "calc"
SKIP_PARTS = {"REFERENCE.patch", "__pycache__", ".pytest_cache"}
IMAGE = "longhaul-sandbox:py312"
PYTEST = "pytest -q"
BASE = "b" * 40
WAIT_S = 30.0
DOCKER_WAIT_S = 300.0  # the calc test builds a virtualenv from PyPI before the first turn
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_acttest_"
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"

CFG = LoopConfig(model="test-model", max_turns=10, max_tokens_per_turn=1000, max_tokens_retry=5000)
# The heartbeat timeouts of section 3.7 are 15 to 60 s. A run in a test shortens them, and the
# activities must still heartbeat inside the shorter one.
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=3)


# ---------- the Temporal server, the database and the blobs ----------
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

    def rows(self, statement: LiteralString) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.url) as conn:
            return conn.execute(statement).fetchall()


def _server_url() -> Any:
    secret = EnvSettings().database_url
    return make_url(secret.get_secret_value() if secret else LOCAL_DSN)


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
    """A fresh database at the head revision. Alembic must not reach the application database."""
    server = _server_url()
    admin_dsn = str(server.set(database="postgres").render_as_string(hide_password=False))
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    url = str(server.set(database=name).render_as_string(hide_password=False))
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", UNREACHABLE_DSN)
    get_settings.cache_clear()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield Scratch(name, url)
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def recorder(database: Scratch) -> AsyncIterator[SqlRecorder]:
    engine = make_engine(database.url)
    try:
        yield SqlRecorder(async_sessionmaker[AsyncSession](engine, expire_on_commit=False))
    finally:
        await engine.dispose()


@pytest.fixture
def blobs() -> BlobStoreBase:
    settings = EnvSettings()
    if settings.blob_endpoint:
        return S3BlobStore.from_settings(settings)
    return MemoryBlobStore()


# ---------- the workspace, which belongs to LH-206 and LH-208 ----------
class Workspace:
    """prepare_workspace and cleanup for the tests. `prepare` makes the sandbox."""

    def __init__(
        self,
        prepare: Callable[[TaskSpec], Awaitable[Sandbox]],
        discard: Callable[[str], Awaitable[None]],
    ) -> None:
        self._prepare, self._discard = prepare, discard
        self.sandboxes: dict[str, Sandbox] = {}

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        async with heartbeating():
            sandbox = await self._prepare(spec)
        self.sandboxes[spec.task_id] = sandbox
        return acts.PrepareWorkspaceOutput(sandbox_id=sandbox.id, base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        await self._discard(inp.task_id)


def fixture_files() -> list[Path]:
    return [
        f
        for f in sorted(CALC.rglob("*"))
        if f.is_file()
        and not SKIP_PARTS & set(f.relative_to(CALC).parts)
        and not any(p.endswith(".egg-info") for p in f.relative_to(CALC).parts)
    ]


async def seed_calc(box: Sandbox) -> None:
    for f in fixture_files():
        await box.write_file(f"/workspace/{f.relative_to(CALC)}", f.read_bytes())


class FakeBoxes:
    """Fake sandboxes by id, in the place of the Docker manager."""

    def __init__(self) -> None:
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    async def prepare(self, spec: TaskSpec) -> Sandbox:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        await seed_calc(box)
        self.boxes[box.id] = box
        return box

    async def discard(self, task_id: str) -> None:
        box = self.boxes.pop(f"sbx-{task_id}", None)
        if box is not None:
            await box.destroy()

    def workspace(self) -> Workspace:
        return Workspace(self.prepare, self.discard)


async def prepare_docker_calc(manager: DockerSandboxManager, spec: TaskSpec) -> Sandbox:
    """Copy calc in, commit it and build its virtualenv, then cut the network."""
    box = await manager.create(spec.task_id, IMAGE, SandboxLimits(), network=True)
    await seed_calc(box)
    committed = await box.exec(
        "git init -q && git add -A && git commit -q -m initial && git tag base", timeout_s=60
    )
    assert committed.exit_code == 0, committed.stderr
    built = await box.exec(
        "python -m venv .venv && "
        ".venv/bin/pip install --disable-pip-version-check --no-cache-dir -q -e '.[dev]'",
        timeout_s=300,
    )
    assert built.exit_code == 0, (
        "could not build the test venv; this test needs outbound network to reach PyPI:\n"
        + built.stderr[-1500:]
    )
    await manager.set_network(box.id, False)
    return box


# ---------- running a task ----------
def make_spec(test_command: str | None = None) -> TaskSpec:
    task_id = f"lh-acme-calc-{uuid.uuid4().hex[:8]}"
    return TaskSpec(
        task_id=task_id,
        repo="acme/calc",
        issue_number=42,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(
            repo="acme/calc", test_command=test_command, budgets=Budgets(task_turns=CFG.max_turns)
        ),
    )


class SpyTools(ToolActivities):
    """ToolActivities that remembers the transcript that each append_results stored."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.transcripts: list[BlobRef] = []

    @activity.defn(name=acts.APPEND_RESULTS.name)
    async def append_results(self, inp: acts.AppendResultsInput) -> BlobRef:
        ref = await super().append_results(inp)
        self.transcripts.append(ref)
        return ref


@dataclass
class Run:
    queue: str
    tools: SpyTools
    blobs: BlobStoreBase

    async def final_transcript(self) -> list[Message]:
        """The transcript after the last turn, which the model never saw but a report reads."""
        return await self.blobs.get_json(self.tools.transcripts[-1], list[Message])


@asynccontextmanager
async def running(
    env: WorkflowEnvironment,
    *,
    provider: ScriptedProvider,
    blobs: BlobStoreBase,
    recorder: SqlRecorder,
    sandboxes: SandboxLookup,
    workspace: Workspace,
    registry: ToolRegistry | None = None,
) -> AsyncIterator[Run]:
    """A worker with the real activities. Yields the task queue it polls."""
    toolbox = registry or default_registry()
    llm = LlmActivities(provider, blobs, recorder, toolbox.specs(), lambda role: CFG)
    tools = SpyTools(toolbox, sandboxes, blobs, recorder)
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[TaskWorkflow],
        activities=[
            *llm.activities,
            *tools.activities,
            workspace.prepare_workspace,
            workspace.cleanup,
        ],
        workflow_runner=workflow_runner(),
    ):
        yield Run(queue, tools, blobs)


async def result_of(handle: WorkflowHandle[Any, Outcome], wait_s: float = WAIT_S) -> Outcome:
    return await asyncio.wait_for(handle.result(), wait_s)


def scheduled(history: WorkflowHistory) -> list[str]:
    """The names of the activities the workflow scheduled, in order."""
    return [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]


def payload_sizes(history: WorkflowHistory) -> list[int]:
    """The size of every payload in the history: inputs, results, failure details and so on."""
    sizes: list[int] = []

    def walk(message: ProtoMessage) -> None:
        if message.DESCRIPTOR.full_name == "temporal.api.common.v1.Payload":
            sizes.append(len(message.data))  # type: ignore[attr-defined]
            return
        for field_, value in message.ListFields():
            if field_.message_type is None:
                continue  # a number, a string or an enum
            if isinstance(value, ProtoMessage):
                walk(value)
            elif field_.message_type.GetOptions().map_entry:
                items = [v for v in value.values() if isinstance(v, ProtoMessage)]
                for item in items:
                    walk(item)
            else:
                for item in value:  # a repeated field
                    walk(item)

    for event in history.events:
        walk(event)
    return sizes


def started_attempts(history: WorkflowHistory, activity_name: str) -> list[int]:
    """The attempt each started activity of this type was on when the history recorded it."""
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


# ---------- LH-204 acceptance criteria ----------
async def test_workflow_fixes_calc_via_activities(
    env: WorkflowEnvironment, database: Scratch, recorder: SqlRecorder, blobs: BlobStoreBase
) -> None:
    manager = DockerSandboxManager()
    spec = make_spec(PYTEST)
    workspace = Workspace(lambda s: prepare_docker_calc(manager, s), manager.cleanup_task)
    provider = ScriptedProvider(SCRIPTS / "calc_fix.yaml")
    try:
        async with running(
            env,
            provider=provider,
            blobs=blobs,
            recorder=recorder,
            sandboxes=manager,
            workspace=workspace,
            registry=default_registry(test_command=PYTEST),
        ) as run:
            handle = await start_task(
                env.client, spec, task_queue=run.queue, tuning=SHORT_HEARTBEAT
            )
            outcome = await result_of(handle, DOCKER_WAIT_S)
            final = await handle.query(TaskWorkflow.status)
            history = await handle.fetch_history()

        box = workspace.sandboxes[spec.task_id]
        assert b"return a + b" in await box.read_file("src/calc/__init__.py")
        tests = await box.exec(PYTEST, timeout_s=60)
        assert tests.exit_code == 0, tests.stdout
        assert "3 passed" in tests.stdout
    finally:
        await manager.cleanup_task(spec.task_id)

    assert outcome == Outcome(
        kind=OutcomeKind.PR_OPENED, summary="Changed add() to return a + b. All tests pass."
    )
    assert (final.phase, final.turn_index) == (Phase.DONE, 5)

    # Every turn and every tool call was an activity: four tool turns, then the finish turn.
    turn = ["llm_turn", "tool_exec", "append_results"]
    assert scheduled(history) == ["prepare_workspace", *turn * 4, "llm_turn", "append_results"]

    turns = database.rows(
        "SELECT turn_index, attempt, stop_reason, input_tokens > 0 FROM turns ORDER BY turn_index"
    )
    assert turns == [(n, 1, "tool_use", True) for n in range(5)]
    tools = database.rows("SELECT name, ok, exit_code FROM tool_calls ORDER BY turn_index")
    assert tools == [
        ("list_dir", True, None),
        ("read_file", True, None),
        ("edit_file", True, None),
        ("run_tests", True, 0),
    ]
    assert max(payload_sizes(history)) < DEFAULT_LIMIT


async def test_history_payloads_small(
    env: WorkflowEnvironment,
    recorder: SqlRecorder,
    blobs: BlobStoreBase,
    tmp_path: Path,
) -> None:
    """A turn with large arguments and large results leaves only small payloads in the history.

    The first reply asks for 31 calls: one file of 40 KB, ten of 15 KB, and twenty commands whose
    output is about 20 KB each. Passed inline, the arguments would be 190 KB and the results
    400 KB, and the results alone would break the 256 KB guard on append_results.
    """
    python = shlex.quote(sys.executable)
    noisy = f"{python} -c \"print('z' * 40000)\""
    first_turn: list[dict[str, Any]] = [
        {"name": "write_file", "input": {"path": "big.txt", "content": "x" * 40_000}},
        *(
            {"name": "write_file", "input": {"path": f"part{n}.txt", "content": "y" * 15_000}}
            for n in range(10)
        ),
        *({"name": "run_command", "input": {"command": noisy}} for _ in range(20)),
    ]
    script = tmp_path / "heavy.yaml"
    script.write_text(
        yaml.safe_dump(
            {
                "steps": [
                    {"match": {"turn_index": 0}, "respond": {"tool_calls": first_turn}},
                    {
                        "match": {"turn_index": 1},
                        "respond": {
                            "tool_calls": [{"name": "finish", "input": {"summary": "Wrote them."}}]
                        },
                    },
                ]
            }
        )
    )
    boxes = FakeBoxes()
    spec = make_spec()
    async with running(
        env,
        provider=ScriptedProvider(script),
        blobs=blobs,
        recorder=recorder,
        sandboxes=boxes,
        workspace=boxes.workspace(),
    ) as run:
        handle = await start_task(env.client, spec, task_queue=run.queue)
        outcome = await result_of(handle)
        history = await handle.fetch_history()
        box = boxes.boxes[f"sbx-{spec.task_id}"]
        assert (await box.read_file("big.txt")).decode() == "x" * 40_000  # args came by ref
        assert (await box.read_file("part0.txt")).decode() == "y" * 15_000  # args inline
        assert (await box.read_file("part9.txt")).decode() == "y" * 15_000  # args by ref

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Wrote them.")
    sizes = payload_sizes(history)
    assert sizes, "the scan found no payloads, so it proves nothing"
    assert max(sizes) < DEFAULT_LIMIT
    # The scan sees inline arguments too: the biggest payload is the reply's summaries.
    assert max(sizes) > 50_000

    # The calls were answered, and the commands' output reached the model whole although it
    # never crossed a boundary inline.
    transcript = await run.final_transcript()
    validate_pairs(transcript)
    uses = [b for m in transcript for b in m.content if isinstance(b, ToolUseBlock)]
    results = [b for m in transcript for b in m.content if isinstance(b, ToolResultBlock)]
    assert len(uses) == len(results) == 32  # 31 calls, then the finish
    commands = [r for r in results if r.content.startswith("exit code: 0")]
    assert len(commands) == 20
    assert all(len(r.content) > 8_000 for r in commands)


class SlowProvider(ScriptedProvider):
    """A scripted provider whose first call takes a while, as a real model call does."""

    def __init__(self, script: Path, delay_s: float) -> None:
        super().__init__(script)
        self.delay_s = delay_s
        self.slow_calls = 1

    async def complete(self, **kwargs: Any) -> LLMResponse:
        if self.slow_calls:
            self.slow_calls -= 1
            await asyncio.sleep(self.delay_s)
        return await super().complete(**kwargs)


async def test_slow_calls_keep_their_heartbeats_inside_a_short_timeout(
    env: WorkflowEnvironment, recorder: SqlRecorder, blobs: BlobStoreBase, tmp_path: Path
) -> None:
    """A model call and a command that outlast the heartbeat timeout are not failed for silence.

    The timeout is 3 s and each of the two calls takes 3.5 s. Only a heartbeat from a background
    task keeps the activity alive, because the call itself sends none. A missed heartbeat would
    show as a second attempt, not as a failed run, since the activity would be retried.
    """
    script = tmp_path / "slow.yaml"
    slow_command = [{"name": "run_command", "input": {"command": "sleep 3.5"}}]
    finish_call = [{"name": "finish", "input": {"summary": "Waited."}}]
    script.write_text(
        yaml.safe_dump(
            {
                "steps": [
                    {"match": {"turn_index": 0}, "respond": {"tool_calls": slow_command}},
                    {"match": {"turn_index": 1}, "respond": {"tool_calls": finish_call}},
                ]
            }
        )
    )
    boxes = FakeBoxes()
    async with running(
        env,
        provider=SlowProvider(script, 3.5),
        blobs=blobs,
        recorder=recorder,
        sandboxes=boxes,
        workspace=boxes.workspace(),
    ) as run:
        handle = await start_task(
            env.client, make_spec(), task_queue=run.queue, tuning=SHORT_HEARTBEAT
        )
        outcome = await result_of(handle)
        history = await handle.fetch_history()

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Waited.")
    assert started_attempts(history, "llm_turn") == [1, 1]
    assert started_attempts(history, "tool_exec") == [1]
    # The history shows what the workflow asked for: the short timeout on the activities that
    # ran for longer than it.
    asked = {
        event.activity_task_scheduled_event_attributes.activity_type.name: (
            event.activity_task_scheduled_event_attributes.heartbeat_timeout.ToTimedelta()
        )
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    }
    assert asked["llm_turn"].total_seconds() == 3.0
    assert asked["tool_exec"].total_seconds() == 3.0


async def test_retries_on_overloaded(
    env: WorkflowEnvironment, database: Scratch, recorder: SqlRecorder, blobs: BlobStoreBase
) -> None:
    provider = ScriptedProvider(SCRIPTS / "activities_overloaded.yaml")
    boxes = FakeBoxes()
    async with running(
        env,
        provider=provider,
        blobs=blobs,
        recorder=recorder,
        sandboxes=boxes,
        workspace=boxes.workspace(),
    ) as run:
        handle = await start_task(env.client, make_spec(), task_queue=run.queue)
        outcome = await result_of(handle)
        history = await handle.fetch_history()

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Finished after two overloads.")
    # Two calls were refused and a third answered, all by the same activity.
    assert len(provider.options_seen) == 3
    assert started_attempts(history, "llm_turn") == [3]
    assert scheduled(history).count("llm_turn") == 1
    # A call that the provider refused before billing leaves no row, so the turn has one.
    assert database.rows("SELECT turn_index, attempt FROM turns") == [(0, 1)]


async def test_invalid_request_not_retried(
    env: WorkflowEnvironment, database: Scratch, recorder: SqlRecorder, blobs: BlobStoreBase
) -> None:
    provider = ScriptedProvider(SCRIPTS / "activities_invalid_request.yaml")
    boxes = FakeBoxes()
    async with running(
        env,
        provider=provider,
        blobs=blobs,
        recorder=recorder,
        sandboxes=boxes,
        workspace=boxes.workspace(),
    ) as run:
        handle = await start_task(env.client, make_spec(), task_queue=run.queue)
        with pytest.raises(WorkflowFailureError) as caught:
            await result_of(handle)
        history = await handle.fetch_history()

    failure = caught.value.cause
    assert isinstance(failure, ActivityError)
    assert failure.retry_state is RetryState.NON_RETRYABLE_FAILURE
    assert isinstance(failure.cause, ApplicationError)
    assert failure.cause.type == "invalid_request"
    assert failure.cause.non_retryable
    # One request went out: the activity was not tried again.
    assert len(provider.options_seen) == 1
    assert started_attempts(history, "llm_turn") == [1]
    assert database.rows("SELECT count(*) FROM turns") == [(0,)]


async def test_a_prompt_over_the_window_ends_the_run_with_context_limit(
    env: WorkflowEnvironment, recorder: SqlRecorder, blobs: BlobStoreBase
) -> None:
    provider = ScriptedProvider(SCRIPTS / "activities_context_too_long.yaml")
    boxes = FakeBoxes()
    async with running(
        env,
        provider=provider,
        blobs=blobs,
        recorder=recorder,
        sandboxes=boxes,
        workspace=boxes.workspace(),
    ) as run:
        handle = await start_task(env.client, make_spec(), task_queue=run.queue)
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="context limit")
    assert (final.phase, final.turn_index) == (Phase.FAILED, 1)
    assert len(provider.options_seen) == 1
