"""Memory in the first request of a task, on a real Postgres with pgvector (LH-604).

A task's workflow calls load_memory once after prepare_workspace, and every model turn reads the
block it wrote. The first test follows that whole path: PgMemoryStore (the embedder is
HashEmbedder, which needs no download), the real load_memory and llm_turn activities, the
workflow on a time skipping Temporal test server, and a scripted provider that keeps the system
prompt of each request. The workspace, the tool calls and the events are stubs, because only the
prompt is in question.

The other tests run the renderer and the worker's start check on the same database. Each test
works on a throwaway database that it creates, migrates to head and drops, so the application
database is never touched.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import build_system_prompt
from longhaul.blobs.base import blob_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.context.tokens import estimate_tokens
from longhaul.contracts import (
    BlobRef,
    MemoryItem,
    Outcome,
    OutcomeKind,
    RepoSettings,
    Role,
    TaskSpec,
)
from longhaul.db.records import MemoryRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts, worker
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.memory import MemoryActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.memory.embedder import EMBEDDING_DIM, HashEmbedder
from longhaul.memory.retrieve import HEADER, MAX_LESSONS, first_test_command, render_memory_block
from longhaul.memory.store import EmbedderMismatchError, PgMemoryStore
from longhaul.providers.scripted import ScriptedProvider
from longhaul.settings import EnvSettings, Memory, get_settings, load_config
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_retrtest_"
# If env.py ever ignored the scratch URL it would fall back to this and fail to connect.
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
REPO = "acme/inventory"
OTHER_REPO = "acme/calc"
SHA = "a" * 40
WAIT_S = 60.0

ISSUE_TITLE = "remove() does nothing for a missing sku"
ISSUE_BODY = "Inventory.remove should raise InventoryError, not ignore a missing sku."
REPO_MAP = "\n".join(
    [
        "Python files of this repo, most relevant first (2 of 2 shown):",
        "inventory/stock.py  # The stock of one warehouse",
        "  class Inventory  # Items by sku",
        "    Inventory.remove(self, sku: str) -> None  # Take one item out",
        "inventory/errors.py",
    ]
)

LESSONS = [
    "Raise InventoryError, not ValueError, when remove gets a missing sku.",
    "The stock dict is keyed by sku, so remove must check the key before it deletes.",
    "Keep the error messages of the inventory short and name the sku.",
    "Name test files test_<module>.py and keep one test file per module.",
    "Format the code with ruff and keep lines under 100 characters.",
    "Write commit messages in the imperative mood.",
    "A missing sku in remove is an error, never a no-op.",
]


QUERY = f"{ISSUE_TITLE}\n\n{ISSUE_BODY}"


def nearest_lessons() -> list[str]:
    """LESSONS by cosine distance to the issue, nearest first, computed here from the vectors."""
    embedder = HashEmbedder()
    [wanted] = embedder.embed([QUERY])

    def closeness(text: str) -> float:
        return sum(a * b for a, b in zip(wanted, embedder.embed([text])[0], strict=True))

    return sorted(LESSONS, key=lambda text: -closeness(text))


# ---------- the database ----------
@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """The URL of a fresh database at the head revision."""
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
        yield url
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def sessions(database: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(database)
    try:
        yield async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def store(sessions: async_sessionmaker[AsyncSession]) -> PgMemoryStore:
    return PgMemoryStore(sessions, HashEmbedder())


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def candidate(content: str, *, repo: str = REPO, kind: str = "lesson") -> MemoryItem:
    return MemoryItem(
        id="", repo=repo, kind=kind, content=content, status="candidate", source="task"
    )  # type: ignore[arg-type]


async def approved_lesson(store: PgMemoryStore, content: str, **fields: Any) -> str:
    item_id = await store.add_candidate(candidate(content, **fields))
    await store.promote(item_id, "merged_pr#7")
    return item_id


async def command_of(store: PgMemoryStore, content: str, *, repo: str = REPO) -> str:
    item = MemoryItem(
        id="",
        repo=repo,
        kind="command",
        content=content,
        status="approved",
        source="default_branch",
    )
    return await store.add_verified(item, source_ref=SHA)


async def fill(store: PgMemoryStore) -> None:
    """A repo with seven approved lessons, two commands, and the notes that must stay out."""
    for lesson in LESSONS:
        await approved_lesson(store, lesson)
    await command_of(store, "pytest -q")
    await command_of(store, "make lint")
    # Written by a task and never approved: the nearest notes to the issue, and out of the prompt.
    await store.add_candidate(candidate("PROPOSED: remove a missing sku silently, it is fine."))
    await store.add_candidate(
        candidate("PROPOSED pitfall: delete the failing test of remove.", kind="pitfall")
    )
    rejected = await store.add_candidate(candidate("REJECTED: remove must ignore a missing sku."))
    await store.reject(rejected, "alice")
    # Another repo's notes, near the issue as well.
    await approved_lesson(
        store, "OTHER REPO: remove raises InventoryError for a missing sku.", repo=OTHER_REPO
    )
    await command_of(store, "OTHER REPO make wipe", repo=OTHER_REPO)


# ---------- the first request of a task ----------
class CapturingProvider(ScriptedProvider):
    """A scripted provider that keeps the system prompt of every request."""

    def __init__(self, script: Path) -> None:
        super().__init__(script)
        self.systems: list[str] = []

    async def complete(self, **kwargs: Any) -> Any:
        self.systems.append(kwargs["system"])
        return await super().complete(**kwargs)


class Stubs:
    """The activities of the task that this test is not about, as a workspace on a worker with a
    memory store would answer."""

    def __init__(self, map_ref: BlobRef) -> None:
        self.map_ref = map_ref

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        return acts.PrepareWorkspaceOutput(
            sandbox_id="sbx-1",
            base_sha=SHA,
            head_sha=SHA,
            repo_map_ref=self.map_ref,
            memory_enabled=True,
            test_command="pytest -q",
        )

    @activity.defn(name=acts.APPEND_RESULTS.name)
    async def append_results(self, inp: acts.AppendResultsInput) -> BlobRef:
        return BlobRef(key=f"tasks/{inp.task_id}/results.json", sha256="0" * 64, size=1)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        return None

    @activity.defn(name=acts.RECORD_EVENT.name)
    async def record_event(self, inp: acts.RecordEventInput) -> None:
        return None


def config_for(role: Role) -> LoopConfig:
    return LoopConfig(
        model="test-model", max_turns=20, max_tokens_per_turn=1000, max_tokens_retry=5000
    )


def spec_of(task_id: str, repo: str = REPO) -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        repo=repo,
        # No issue number: a task of an issue on an owner/name repo is delivered as a pull request.
        issue_number=None,
        title=ISSUE_TITLE,
        body=ISSUE_BODY,
        settings=RepoSettings(repo=repo),
    )


@asynccontextmanager
async def running(
    env: WorkflowEnvironment,
    store: PgMemoryStore,
    blobs: MemoryBlobStore,
    provider: CapturingProvider,
    map_ref: BlobRef,
) -> AsyncIterator[str]:
    stubs = Stubs(map_ref)
    llm = LlmActivities(provider, blobs, MemoryRecorder(), default_registry().specs(), config_for)
    memory = MemoryActivities(store, blobs)
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[TaskWorkflow],
        activities=[
            *llm.activities,
            *memory.activities,
            stubs.prepare_workspace,
            stubs.append_results,
            stubs.cleanup,
            stubs.record_event,
        ],
        workflow_runner=workflow_runner(),
    ):
        yield queue


def finish_at_once(tmp_path: Path) -> Path:
    step = {
        "match": {"role": "coder", "turn_index": 0},
        "respond": {"tool_calls": [{"name": "finish", "input": {"summary": "Fixed remove."}}]},
    }
    path = tmp_path / "finish.yaml"
    path.write_text(yaml.safe_dump({"steps": [step]}))
    return path


async def test_a_tasks_first_request_carries_the_block(
    env: WorkflowEnvironment, store: PgMemoryStore, tmp_path: Path
) -> None:
    await fill(store)
    blobs = MemoryBlobStore()
    spec = spec_of(f"lh-acme-inventory-{uuid.uuid4().hex[:8]}")
    map_ref = await blobs.put(
        REPO_MAP.encode(), key=blob_key(spec.task_id, "memory", 0, "repo-map.txt")
    )
    provider = CapturingProvider(finish_at_once(tmp_path))

    async with running(env, store, blobs, provider, map_ref) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        outcome = await asyncio.wait_for(handle.result(), WAIT_S)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Fixed remove.")
    (system,) = provider.systems
    block = (
        await blobs.get_by_key(blob_key(spec.task_id, "memory", 0, "memory-block.txt"))
    ).decode()

    # The first request is the system prompt with the block as its block 4, and the test command
    # that prepare_workspace verified in the task statement.
    with_command = spec.model_copy(
        update={"settings": RepoSettings(repo=REPO, test_command="pytest -q")}
    )
    assert system == build_system_prompt(Role.CODER, with_command, block)
    assert "Test command: pytest -q" in system
    assert system.endswith(block)
    assert block.startswith(HEADER)
    assert estimate_tokens(block) <= 4000

    # The map of the workspace, the commands of the repo, and the five lessons nearest to the
    # issue, of the seven that are approved.
    assert "inventory/stock.py" in block
    assert "- pytest -q" in block and "- make lint" in block
    kept, left_out = nearest_lessons()[:MAX_LESSONS], nearest_lessons()[MAX_LESSONS:]
    assert [line for line in block.splitlines() if line.startswith("- [lesson] ")] == [
        f"- [lesson] {text}" for text in kept
    ]
    assert not [text for text in left_out if text in block]

    # I7: nothing that a task proposed and nobody approved, and nothing of another repo.
    for banned in ("PROPOSED", "REJECTED", "OTHER REPO"):
        assert banned not in system

    # The lessons that reached the prompt are counted as used, and the others are not.
    used = {e.content: e.uses for e in await store.list_items(REPO, kind="lesson")}
    assert {text: used[text] for text in LESSONS} == {
        **{text: 1 for text in kept},
        **{text: 0 for text in left_out},
    }


# ---------- the renderer on the real store ----------
async def test_the_nearest_five_approved_lessons_come_back_nearest_first(
    store: PgMemoryStore,
) -> None:
    await fill(store)

    block = await render_memory_block(store, REPO, QUERY)

    lessons = [line for line in block.splitlines() if line.startswith("- [lesson] ")]
    # By cosine distance of the real vectors, nearest first, and five of the seven.
    assert [line.removeprefix("- [lesson] ") for line in lessons] == nearest_lessons()[:MAX_LESSONS]
    assert nearest_lessons()[0] in (LESSONS[0], LESSONS[6])


async def test_a_note_turns_up_in_the_block_once_a_person_approves_it_and_not_before(
    store: PgMemoryStore,
) -> None:
    item_id = await store.add_candidate(candidate("Run the linter before you finish remove."))
    issue = "run the linter for remove"

    assert await render_memory_block(store, REPO, issue) == ""
    await store.promote(item_id, "alice")
    assert "- [lesson] Run the linter before you finish remove." in await render_memory_block(
        store, REPO, issue
    )
    await store.reject(item_id, "alice")
    assert await render_memory_block(store, REPO, issue) == ""


async def test_the_test_command_is_the_first_approved_command_that_runs_tests(
    store: PgMemoryStore,
) -> None:
    assert await first_test_command(store, REPO) is None
    await command_of(store, "make lint")
    assert await first_test_command(store, REPO) is None
    await command_of(store, "pytest -q")
    await command_of(store, "tox")
    other = await store.add_candidate(candidate("pytest --candidate", kind="command"))

    assert await first_test_command(store, REPO) == "pytest -q"
    assert await first_test_command(store, OTHER_REPO) is None
    # A person turns it down, and the next approved one takes its place.
    [entry] = [e for e in await store.list_items(REPO, kind="command") if e.content == "pytest -q"]
    await store.reject(entry.id, "alice")
    assert await first_test_command(store, REPO) == "tox"
    assert other  # the candidate never counted


async def test_search_can_be_limited_to_kinds(store: PgMemoryStore) -> None:
    await approved_lesson(store, "A lesson about remove.")
    await approved_lesson(store, "A pitfall about remove.", kind="pitfall")
    await command_of(store, "pytest -q")

    found = await store.search(REPO, "remove", k=10, kinds=("lesson", "pitfall"))

    assert sorted(item.kind for item in found) == ["lesson", "pitfall"]
    assert len(await store.search(REPO, "remove", k=10)) == 3


# ---------- one embedder for the whole table (ADR-0010) ----------
class ReversedHashEmbedder:
    """Another embedder: the same hash with its numbers in the other order.

    It has the right width and unit length, so the table takes its vectors, and they lie nowhere
    near the ones of HashEmbedder, as with two real models.
    """

    dim = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector[::-1] for vector in HashEmbedder().embed(texts)]


async def test_an_empty_table_takes_any_embedder(store: PgMemoryStore) -> None:
    await store.check_embedder()


async def test_the_embedder_that_wrote_the_table_may_read_it(store: PgMemoryStore) -> None:
    await fill(store)

    await store.check_embedder()


async def test_another_embedder_than_the_one_that_wrote_the_table_is_refused(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await fill(PgMemoryStore(sessions, HashEmbedder()))

    with pytest.raises(EmbedderMismatchError, match="memory.embedder"):
        await PgMemoryStore(sessions, ReversedHashEmbedder()).check_embedder()  # type: ignore[arg-type]


async def test_a_worker_with_the_wrong_embedder_refuses_to_start(
    database: str, sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    # The table was written by another embedder, as after a change of memory.embedder.
    await fill(PgMemoryStore(sessions, ReversedHashEmbedder()))  # type: ignore[arg-type]
    settings = EnvSettings(
        _env_file=None,
        database_url=database,
        blob_endpoint="http://127.0.0.1:1",
        blob_bucket="nothing",
        blob_access_key="key",
        blob_secret_key="secret",
        ckpt_dir=tmp_path / "ckpt",
    )
    config = load_config(ROOT / "config" / "longhaul.example.yaml").model_copy(
        update={"memory": Memory(enabled=True, embedder="hash")}
    )
    parts = worker.build_parts(settings, config, object())  # type: ignore[arg-type]
    try:
        with pytest.raises(ValueError, match="memory.embedder"):
            await parts.check_memory()
    finally:
        await parts.close()
