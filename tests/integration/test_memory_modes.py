"""The memory mode: `on` puts notes in the prompt, `off` leaves them out (LH-607).

`memory.mode: off` is the switch for the ablation runs of Phase 9 and for an operator who does
not trust the notes of a repo. The block of the prompt stays empty, and the verified commands
still set the test command of the task, since they are Longhaul's own findings at the default
branch and not notes written by a task.

The tests build the parts of a worker from a config, as `longhaul worker` does, and run the
real load_memory and llm_turn activities in the workflow, on a Temporal test server and a real
Postgres with pgvector. The workspace is a stub, except that its test command is read by the
real prepare_workspace code from the same store. Each test works on a throwaway database that
the fixtures of test_memory_retrieval.py create, migrate to head and drop.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.integration.test_memory_retrieval import (  # noqa: F401  (fixtures)
    LESSONS,
    REPO,
    ROOT,
    SHA,
    WAIT_S,
    CapturingProvider,
    command_of,
    config_for,
    database,
    env,
    fill,
    finish_at_once,
    sessions,
    spec_of,
    store,
)

from longhaul.agent.prompts import build_system_prompt
from longhaul.blobs.base import blob_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import BlobRef, RepoSettings, Role, TaskSpec
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts, worker
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.memory.retrieve import HEADER
from longhaul.memory.store import PgMemoryStore
from longhaul.settings import EnvSettings, Memory, load_config
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase6]

REPO_MAP = "inventory/stock.py  # The stock of one warehouse"


class Parts:
    """The parts of a worker built from a config, and the stubs that stand in for the rest.

    The memory and workspace activities are the ones `build_parts` made. Their blob store is the
    one thing swapped, since the worker's S3 client points at nothing in these tests.
    """

    def __init__(self, parts: worker.WorkerParts, blobs: MemoryBlobStore) -> None:
        self.parts = parts
        self.blobs = blobs
        self.loader = one(parts, "load_memory")
        self.workspace = one(parts, "prepare_workspace")
        self.loader._blobs = blobs

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        # The worker's own answer to "what runs the tests here", read from its store. The map is
        # stood in for by a blob, because the real one needs a sandbox.
        map_ref = None
        if self.workspace.repo_maps is not None:
            key = blob_key(spec.task_id, "memory", 0, "repo-map.txt")
            map_ref = await self.blobs.put(REPO_MAP.encode(), key=key)
        return acts.PrepareWorkspaceOutput(
            sandbox_id="sbx-1",
            base_sha=SHA,
            head_sha=SHA,
            repo_map_ref=map_ref,
            memory_enabled=self.parts.memory is not None,
            test_command=await self.workspace._test_command(spec),
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


def one(parts: worker.WorkerParts, name: str) -> Any:
    (found,) = {fn.__self__ for fn in parts.activities if getattr(fn, "__name__", "") == name}
    return found


def build(
    database: str,  # noqa: F811
    tmp_path: Path,
    **memory: Any,
) -> worker.WorkerParts:
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
        update={"memory": Memory(enabled=True, embedder="hash", **memory)}
    )
    return worker.build_parts(settings, config, object())  # type: ignore[arg-type]


async def first_request(
    env: WorkflowEnvironment,  # noqa: F811
    stubs: Parts,
    tmp_path: Path,
) -> tuple[TaskSpec, str, str]:
    """Run one task to its end. Return its spec, the system prompt of its first request, and the
    memory block that the prompt was built from."""
    provider = CapturingProvider(finish_at_once(tmp_path))
    llm = LlmActivities(
        provider, stubs.blobs, MemoryRecorder(), default_registry().specs(), config_for
    )
    spec = spec_of(f"lh-acme-inventory-{uuid.uuid4().hex[:8]}")
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[TaskWorkflow],
        activities=[
            *llm.activities,
            stubs.loader.load_memory,
            stubs.prepare_workspace,
            stubs.append_results,
            stubs.cleanup,
            stubs.record_event,
        ],
        workflow_runner=workflow_runner(),
    ):
        handle = await start_task(env.client, spec, task_queue=queue)
        try:
            await asyncio.wait_for(handle.result(), WAIT_S)
        except WorkflowFailureError as exc:  # show the cause, not the wrapper
            raise AssertionError(f"the task failed: {exc.cause!r}") from exc
    (system,) = provider.systems
    key = blob_key(spec.task_id, "memory", 0, "memory-block.txt")
    return spec, system, (await stubs.blobs.get_by_key(key)).decode()


async def test_on_mode(
    env: WorkflowEnvironment,  # noqa: F811
    store: PgMemoryStore,  # noqa: F811
    database: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The control: with mode on, the same store puts its notes in the prompt."""
    await fill(store)
    parts = build(database, tmp_path, mode="on")
    try:
        _, system, block = await first_request(env, Parts(parts, MemoryBlobStore()), tmp_path)
    finally:
        await parts.close()

    assert block.startswith(HEADER)
    assert system.endswith(block)
    assert any(text in system for text in LESSONS)
    assert REPO_MAP in system
    assert "Test command: pytest -q" in system


async def test_off_mode(
    env: WorkflowEnvironment,  # noqa: F811
    store: PgMemoryStore,  # noqa: F811
    database: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    await fill(store)
    parts = build(database, tmp_path, mode="off")
    stubs = Parts(parts, MemoryBlobStore())
    try:
        spec, system, block = await first_request(env, stubs, tmp_path)
    finally:
        await parts.close()

    # No memory block: the block is empty and the prompt is the one of a repo with no notes,
    # apart from the verified test command in the task statement.
    assert block == ""
    verified = spec.model_copy(
        update={"settings": RepoSettings(repo=REPO, test_command="pytest -q")}
    )
    assert system == build_system_prompt(Role.CODER, verified, "")
    assert "Test command: pytest -q" in system
    assert HEADER not in system
    assert not [text for text in LESSONS if text in system]
    assert REPO_MAP not in system
    assert "make lint" not in system, "only the test command is kept, not the other commands"

    # The store was not asked for notes at all, so no lesson is counted as used.
    lessons = await store.list_items(REPO, kind="lesson")
    assert {item.uses for item in lessons} == {0}


async def test_off_mode_still_learns_a_test_command_from_the_verified_commands(
    store: PgMemoryStore,  # noqa: F811
    database: str,  # noqa: F811
    tmp_path: Path,
) -> None:
    await command_of(store, "make lint")
    await command_of(store, "pytest -q")
    parts = build(database, tmp_path, mode="off")
    try:
        workspace = one(parts, "prepare_workspace")
        spec = spec_of("lh-acme-inventory-modes")
        assert await workspace._test_command(spec) == "pytest -q"
        # A repo that names its own test command keeps it.
        own = spec.model_copy(update={"settings": RepoSettings(repo=REPO, test_command="tox")})
        assert await workspace._test_command(own) is None
    finally:
        await parts.close()
