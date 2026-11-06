"""Sandbox recovery and retry-safe tool calls on real containers (LH-206, failure modes F1 and F3).

prepare_workspace, ensure_sandbox, tool_exec and the checkpoint store run for real here, under a
TaskWorkflow on a time skipping Temporal test server. The provider is scripted. The repo is a
plain folder, a copy of the calc fixture, so prepare_workspace has to turn it into a repo. The
sandboxes are Docker containers, the bare repos live under pytest's tmp_path, and the recorder
writes to a throwaway Postgres database (migrated to head, then dropped).

The tests that remove containers do it from outside, with the Docker SDK, the way
`docker rm -f` does. The test of a worker that dies in the middle of a tool call injects the
failure into the checkpoint store, after the tool has run, so the activity is retried by Temporal
and not by the test.

Every task, container, volume, task queue and scratch database has a name with a random suffix,
and each test removes what it made. The default setup builds a virtualenv from PyPI, so the
machine needs outbound network, as the other sandbox tests do.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, LiteralString

import docker
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import WorkflowHandle, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.agent.workspace import collect_source_files
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Outcome,
    OutcomeKind,
    Phase,
    RepoSettings,
    Sandbox,
    SandboxLimits,
    TaskSpec,
)
from longhaul.db.records import SqlRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import ToolActivities
from longhaul.durable.activities.workspace import SANDBOX_RECREATED, WorkspaceActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.docker_manager import TASK_LABEL, DockerSandboxManager, volume_name
from longhaul.settings import EnvSettings, get_settings
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
SCRIPTS = ROOT / "tests" / "fixtures" / "scripts"
IMAGE = "longhaul-sandbox:py312"
PYTEST = "pytest -q"
MODULE = "src/calc/__init__.py"
# The default setup builds a virtualenv from PyPI before the first turn, and again after a
# recovery that lost the volume.
DOCKER_WAIT_S = 600.0
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_rectest_"
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"
CFG = LoopConfig(model="test-model", max_turns=10, max_tokens_per_turn=1000, max_tokens_retry=5000)
# The heartbeat timeouts of section 3.7 are 15 to 60 s. A run in a test shortens them, and the
# activities must still heartbeat inside the shorter one.
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=3)


# ---------- the Temporal server, the database and the host ----------
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


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[Scratch]:
    """A fresh database at the head revision. Alembic must not reach the application database."""
    secret = EnvSettings().database_url
    server = make_url(secret.get_secret_value() if secret else LOCAL_DSN)
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


def host_git(*args: str) -> str:
    """Git on the host, as the tests read a repo: no inherited GIT_* variables."""
    done = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=plain_env(), timeout=60
    )
    return done.stdout.strip()


def plain_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def containers_of(task_id: str, *, all_states: bool = True) -> list[Any]:
    """The containers that Docker has for a task, found by the label the manager sets."""
    return docker.from_env().containers.list(
        all=all_states, filters={"label": f"{TASK_LABEL}={task_id}"}
    )


def remove_containers(task_id: str, *, volume: bool = False) -> None:
    """`docker rm -f` on every container of the task, and `docker volume rm` when asked."""
    for container in containers_of(task_id):
        container.remove(force=True)
    if volume:
        docker.from_env().volumes.get(volume_name(task_id)).remove(force=True)


def networks(container: Any) -> set[str]:
    container.reload()
    return set(container.attrs["NetworkSettings"]["Networks"])


# ---------- one task on a real stack ----------
@dataclass
class Stack:
    """The pieces of a worker for one task, and the task's folder and spec."""

    task_id: str
    source: Path
    manager: DockerSandboxManager
    store: GitCheckpointStore
    blobs: MemoryBlobStore
    recorder: SqlRecorder
    tmp_path: Path
    checkpoints: Any = None  # what tool_exec gets; the store unless a test wraps it
    queue: str = field(default_factory=lambda: f"lh-test-{uuid.uuid4().hex[:8]}")

    @property
    def workspace(self) -> WorkspaceActivities:
        return WorkspaceActivities(
            self.manager, self.store, self.recorder, image=IMAGE, limits=SandboxLimits()
        )

    def spec(self, **settings: Any) -> TaskSpec:
        repo = f"local:{self.source}"
        return TaskSpec(
            task_id=self.task_id,
            repo=repo,
            issue_number=42,
            title="add() subtracts",
            body="`calc.add` subtracts its arguments instead of adding them.",
            settings=RepoSettings(
                repo=repo, test_command=PYTEST, budgets=Budgets(task_turns=10), **settings
            ),
        )

    @property
    def branch(self) -> str:
        return "longhaul/42"

    def bare(self, *args: str) -> str:
        return host_git(f"--git-dir={self.store.repo_path(self.task_id)}", *args)

    def head(self) -> str:
        return self.bare("rev-parse", f"refs/heads/{self.branch}")

    def tree_at(self, sha: str) -> dict[str, bytes]:
        """Every file of a commit in the bare repo, by path."""
        names = self.bare("ls-tree", "-r", "-z", "--name-only", sha).split("\0")
        repo = self.store.repo_path(self.task_id)
        return {
            name: subprocess.run(
                ["git", f"--git-dir={repo}", "show", f"{sha}:{name}"],
                check=True,
                capture_output=True,
                env=plain_env(),
                timeout=60,
            ).stdout
            for name in names
            if name
        }

    async def live_sandbox(self) -> Sandbox:
        (container,) = containers_of(self.task_id, all_states=False)
        sandbox = await self.manager.get(container.id)
        assert sandbox is not None
        return sandbox


@pytest.fixture
async def stack(tmp_path: Path, recorder: SqlRecorder) -> AsyncIterator[Stack]:
    # The id ends in the issue number, so the scratch branch is longhaul/42.
    task_id = f"lh-rec{uuid.uuid4().hex[:8]}-42"
    source = shutil.copytree(CALC, tmp_path / "calc")
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    manager = DockerSandboxManager()
    built = Stack(task_id, source, manager, store, blobs, recorder, tmp_path)
    built.checkpoints = store
    try:
        yield built
    finally:
        await manager.cleanup_task(task_id)
        await store.cleanup_task(task_id)


# ---------- a worker with the real activities ----------
@asynccontextmanager
async def running(
    env: WorkflowEnvironment, stack: Stack, provider: ScriptedProvider
) -> AsyncIterator[str]:
    """A worker for the workflow and the real activities. Yields the task queue it polls."""
    registry = default_registry(test_command=PYTEST, blobs=stack.blobs)
    llm = LlmActivities(provider, stack.blobs, stack.recorder, registry.specs(), lambda role: CFG)
    tools = ToolActivities(
        registry, stack.manager, stack.blobs, stack.recorder, checkpoints=stack.checkpoints
    )
    async with Worker(
        env.client,
        task_queue=stack.queue,
        workflows=[TaskWorkflow],
        activities=[
            *llm.activities,
            *tools.activities,
            *stack.workspace.activities,  # includes the real cleanup
            *EventActivities(stack.recorder).activities,
        ],
        workflow_runner=workflow_runner(),
    ):
        yield stack.queue


async def result_of(handle: WorkflowHandle[Any, Outcome], wait_s: float = DOCKER_WAIT_S) -> Outcome:
    return await asyncio.wait_for(handle.result(), wait_s)


def scheduled(history: WorkflowHistory) -> list[str]:
    """The names of the activities the workflow scheduled, in order."""
    return [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    ]


def failures(history: WorkflowHistory) -> list[str]:
    """The error type of each activity attempt that failed for good, in order."""
    return [
        event.activity_task_failed_event_attributes.failure.application_failure_info.type
        for event in history.events
        if event.HasField("activity_task_failed_event_attributes")
    ]


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


def reference_tree(source: Path, into: Path) -> dict[str, bytes]:
    """The files of the repo after the reference patch: what a correct fix leaves behind."""
    for item in collect_source_files(source):
        target = into / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / item.path, target)
    patched = subprocess.run(
        ["patch", "-p1", "-d", str(into), "-i", str(source / "REFERENCE.patch")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert patched.returncode == 0, patched.stdout + patched.stderr
    return {
        p.relative_to(into).as_posix(): p.read_bytes()
        for p in sorted(into.rglob("*"))
        if p.is_file()
    }


class KillsTheSandbox(ScriptedProvider):
    """A scripted provider that removes the task's containers just before it answers a turn.

    Turn `at` is the model call after the calls of turn `at - 1` have been answered, so the
    container goes while the workflow is between two tool calls, as it does when someone runs
    `docker rm -f` on the machine.
    """

    def __init__(self, script: Path, *, at: int, task_id: str, volume: bool = False) -> None:
        super().__init__(script)
        self.at, self.task_id, self.volume = at, task_id, volume
        self.killed = False

    async def complete(self, **kwargs: Any) -> LLMResponse:
        turns = sum(1 for m in kwargs["messages"] if m.role == "assistant")
        if turns == self.at and not self.killed:
            self.killed = True
            await asyncio.to_thread(remove_containers, self.task_id, volume=self.volume)
        return await super().complete(**kwargs)


# ---------- prepare_workspace on Docker ----------
async def test_prepare_workspace_builds_a_sandbox_from_a_local_folder(stack: Stack) -> None:
    # The setup reaches PyPI, so it only passes while the network is on. The network is cut after.
    online = (
        "python -c \"import urllib.request as u; u.urlopen('https://pypi.org', timeout=20)\""
        " && mkdir -p .venv && echo ran >> .venv/setup-log"
    )
    spec = stack.spec(setup_command=online)

    env = ActivityEnvironment()
    out = await env.run(stack.workspace.prepare_workspace, spec)

    (container,) = containers_of(stack.task_id)
    assert container.id == out.sandbox_id
    assert container.status == "running"
    assert networks(container) == set(), "the network is cut once the setup is done"
    mounts = {m["Destination"]: m for m in container.attrs["Mounts"] if m["Type"] == "bind"}
    assert set(mounts) == {"/ckpt"}
    assert mounts["/ckpt"]["Source"].endswith(f"{stack.task_id}.git")

    box = await stack.manager.get(out.sandbox_id)
    assert box is not None
    git = await box.exec("git rev-parse HEAD && git rev-parse refs/tags/base", timeout_s=60)
    assert git.stdout.split() == [out.base_sha, out.base_sha], git.stderr
    assert out.head_sha == out.base_sha == stack.head()
    assert (await box.exec("cat .venv/setup-log", timeout_s=60)).stdout == "ran\n"
    offline = await box.exec(
        "python -c \"import urllib.request as u; u.urlopen('https://pypi.org', timeout=5)\"",
        timeout_s=30,
    )
    assert offline.exit_code != 0
    # The files are the ones the run-local copy rules keep: not the reference fix, not the issue.
    files = (await box.exec("git ls-files", timeout_s=60)).stdout.split()
    assert MODULE in files and "pyproject.toml" in files
    assert "REFERENCE.patch" not in files and "ISSUE.md" not in files
    assert stack.tree_at(out.base_sha).keys() == set(files)


async def test_a_retried_prepare_workspace_leaves_one_live_container(stack: Stack) -> None:
    # An attempt that died halfway: its container still runs, with the network on, over a
    # volume that holds a half-installed workspace. The retry has to start from nothing.
    env = ActivityEnvironment()
    spec = stack.spec(setup_command="")
    dead = await stack.manager.create(
        stack.task_id, IMAGE, SandboxLimits(), network=True, mounts=None
    )
    await dead.write_file("half-installed.txt", b"x\n")

    out = await env.run(stack.workspace.prepare_workspace, spec)

    (container,) = containers_of(stack.task_id)
    assert container.id == out.sandbox_id != dead.id
    box = await stack.manager.get(out.sandbox_id)
    assert box is not None
    listing = (await box.exec("ls -A", timeout_s=30)).stdout.split()
    assert "half-installed.txt" not in listing and "pyproject.toml" in listing

    # And a second retry on top of a finished attempt does the same.
    again = await ActivityEnvironment().run(stack.workspace.prepare_workspace, spec)
    (container,) = containers_of(stack.task_id)
    assert container.id == again.sandbox_id != out.sandbox_id
    assert again.base_sha == out.base_sha


async def test_remove_containers_keeps_the_workspace_volume(stack: Stack) -> None:
    box = await stack.manager.create(stack.task_id, IMAGE, SandboxLimits(), network=False)
    await box.write_file("kept.txt", b"still here\n")

    await stack.manager.remove_containers(stack.task_id)

    assert containers_of(stack.task_id) == []
    docker.from_env().volumes.get(volume_name(stack.task_id))  # raises NotFound if it is gone
    again = await stack.manager.create(stack.task_id, IMAGE, SandboxLimits(), network=False)
    assert await again.read_file("kept.txt") == b"still here\n"
    await stack.manager.remove_containers(stack.task_id)  # twice is fine
    await stack.manager.remove_containers("lh-never-existed")


# ---------- ensure_sandbox on Docker ----------
async def test_ensure_sandbox_keeps_a_running_sandbox_and_replaces_a_stopped_one(
    stack: Stack,
) -> None:
    out = await ActivityEnvironment().run(
        stack.workspace.prepare_workspace, stack.spec(setup_command="")
    )

    def ensure(sandbox_id: str | None) -> Any:
        inp = acts.EnsureSandboxInput(
            task_id=stack.task_id,
            head_sha=out.head_sha,
            sandbox_id=sandbox_id,
            spec=stack.spec(setup_command=""),
        )
        return ActivityEnvironment().run(stack.workspace.ensure_sandbox, inp)

    assert await ensure(out.sandbox_id) == out.sandbox_id

    # A container that exists and does not run is not a sandbox. Exactly one is left after.
    (old,) = containers_of(stack.task_id)
    old.stop(timeout=1)
    new_id = await ensure(out.sandbox_id)
    assert new_id != out.sandbox_id
    (only,) = containers_of(stack.task_id)
    assert only.id == new_id and only.status == "running"
    assert networks(only) == set()


# ---------- the acceptance criteria ----------
async def test_container_removed_is_recreated_at_head(
    env: WorkflowEnvironment, stack: Stack, database: Scratch
) -> None:
    """Given a running task, when its container is removed, the next tool call recreates it at
    head_sha and the task finishes with the reference diff."""
    provider = KillsTheSandbox(SCRIPTS / "calc_fix.yaml", at=3, task_id=stack.task_id)
    async with running(env, stack, provider) as queue:
        handle = await start_task(
            env.client, stack.spec(), task_queue=queue, tuning=SHORT_HEARTBEAT
        )
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)
        history = await handle.fetch_history()

    assert provider.killed
    assert outcome == Outcome(
        kind=OutcomeKind.PR_OPENED, summary="Changed add() to return a + b. All tests pass."
    )
    assert (final.phase, final.turn_index) == (Phase.DONE, 5)

    # The fourth tool call found the container gone, the sandbox was rebuilt, and the same call
    # ran again: list_dir, read_file, edit_file, run_tests (lost), run_tests.
    names = scheduled(history)
    assert names.count("prepare_workspace") == 1
    assert names.count("ensure_sandbox") == 1
    assert names.count("tool_exec") == 5
    assert failures(history) == [acts.SANDBOX_GONE]

    # The recreation is on record, at the checkpoint of the edit.
    (edit_sha,) = database.rows("SELECT post_sha FROM tool_calls WHERE name = 'edit_file'")[0]
    assert edit_sha is not None
    events = database.rows("SELECT type, payload FROM events ORDER BY id")
    # The run ends with the note that the metrics count the task from (LH-704).
    assert [kind for kind, _ in events] == [SANDBOX_RECREATED, acts.OUTCOME_EVENT]
    payload = events[0][1]
    assert payload["head_sha"] == edit_sha
    (container,) = containers_of(stack.task_id)
    assert payload["sandbox_id"] == container.id != payload["previous_sandbox_id"]

    # One live container is left, and the work in it is the reference diff: the files at the
    # final checkpoint are the files of the repo with the reference patch applied.
    assert container.status == "running" and networks(container) == set()
    expected = reference_tree(CALC, stack.tmp_path / "reference")
    assert stack.tree_at(stack.head()) == expected
    box = await stack.live_sandbox()
    assert (await box.exec("git status --porcelain", timeout_s=60)).stdout == ""
    tests = await box.exec(PYTEST, timeout_s=120)
    assert tests.exit_code == 0 and "3 passed" in tests.stdout, tests.stdout


async def test_container_and_volume_removed_is_rebuilt_at_head(
    env: WorkflowEnvironment, stack: Stack, database: Scratch
) -> None:
    # F3 at its worst: the workspace volume goes with the container. The new container is a
    # clone of the checkpoint branch, and the setup builds the virtualenv again.
    provider = KillsTheSandbox(SCRIPTS / "calc_fix.yaml", at=3, task_id=stack.task_id, volume=True)
    async with running(env, stack, provider) as queue:
        handle = await start_task(
            env.client, stack.spec(), task_queue=queue, tuning=SHORT_HEARTBEAT
        )
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    # The recreation, and the note at the end of the run (LH-704).
    assert database.rows("SELECT type FROM events ORDER BY id") == [
        (SANDBOX_RECREATED,),
        (acts.OUTCOME_EVENT,),
    ]
    assert stack.tree_at(stack.head()) == reference_tree(CALC, stack.tmp_path / "reference")
    (container,) = containers_of(stack.task_id)
    assert container.status == "running"


# ---------- a tool call that is retried ----------
EDIT_ONCE = """\
steps:
  - respond:
      tool_calls:
        - name: edit_file
          input:
            path: src/calc/__init__.py
            old_str: "    return a - b"
            new_str: "    return a - b + 1"
  - respond:
      tool_calls:
        - {name: finish, input: {summary: "Edited once."}}
"""


class DiesOnce:
    """A checkpoint store that passes every call to the real one, and dies once as a worker does.

    `where` is "before": the tool has run and the edit is in the workspace, and the worker is
    killed before the commit. Or "after": the commit is made and pushed, and the worker is killed
    before the result goes back to Temporal.
    """

    def __init__(self, store: GitCheckpointStore, where: str) -> None:
        self.store, self.where = store, where
        self.seen_at_death: str | None = None
        self.died = False

    async def init_task(self, task_id: str, clone_url: str, base_sha: str) -> str:
        return await self.store.init_task(task_id, clone_url, base_sha)

    async def commit(
        self, sandbox: Sandbox, message: str, *, parent: str | None = None
    ) -> str | None:
        if self.where == "before" and not self.died:
            await self._die(sandbox)
        sha = await self.store.commit(sandbox, message, parent=parent)
        if self.where == "after" and not self.died:
            await self._die(sandbox)
        return sha

    async def _die(self, sandbox: Sandbox) -> None:
        # What the retry has to deal with: the edit is applied in the workspace.
        self.seen_at_death = (await sandbox.read_file(MODULE)).decode()
        self.died = True
        raise RuntimeError("the worker was killed")

    async def restore(self, sandbox: Sandbox, sha: str) -> None:
        await self.store.restore(sandbox, sha)

    async def bundle(self, task_id: str) -> BlobRef:
        return await self.store.bundle(task_id)

    async def is_at(self, sandbox: Sandbox, sha: str) -> bool:
        return await self.store.is_at(sandbox, sha)


async def run_edit_once(
    env: WorkflowEnvironment, stack: Stack, where: str, database: Scratch
) -> tuple[DiesOnce, WorkflowHistory, str]:
    script = stack.tmp_path / "edit_once.yaml"
    script.write_text(EDIT_ONCE)
    dies = DiesOnce(stack.store, where)
    stack.checkpoints = dies
    async with running(env, stack, ScriptedProvider(script)) as queue:
        handle = await start_task(
            env.client, stack.spec(setup_command=""), task_queue=queue, tuning=SHORT_HEARTBEAT
        )
        outcome = await result_of(handle)
        history = await handle.fetch_history()
    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Edited once.")
    assert dies.died
    base = stack.bare("rev-parse", "refs/tags/base")
    return dies, history, base


def check_applied_once(stack: Stack, dies: DiesOnce, history: WorkflowHistory, base: str) -> None:
    # The first attempt did apply the edit, so the retry started from a half-changed workspace.
    assert dies.seen_at_death is not None and dies.seen_at_death.count("+ 1") == 1
    # Temporal ran the call again: the edit is on its second attempt.
    assert started_attempts(history, "tool_exec") == [2]
    # The file has the change once, in the checkpoint and in the workspace that is left.
    head = stack.head()
    final = stack.tree_at(head)[MODULE].decode()
    assert final.count("+ 1") == 1 and "return a - b + 1" in final
    # And the checkpoint chain is a line: the base and one commit for the call.
    assert stack.bare("rev-list", "--count", f"refs/heads/{stack.branch}") == "2"
    assert stack.bare("rev-list", "--merges", f"refs/heads/{stack.branch}") == ""
    assert stack.bare("rev-parse", f"{head}^") == base
    assert stack.bare("log", "-1", "--format=%s", head).startswith("lh: edit_file turn 0 call ")


async def test_retry_after_partial_edit_applies_once(
    env: WorkflowEnvironment, stack: Stack, database: Scratch
) -> None:
    """A tool_exec that was killed after editing but before returning is retried by Temporal,
    and the edit is applied exactly once."""
    dies, history, base = await run_edit_once(env, stack, "before", database)

    check_applied_once(stack, dies, history, base)
    box = await stack.live_sandbox()
    assert (await box.read_file(MODULE)).decode().count("+ 1") == 1
    assert (await box.exec("git status --porcelain", timeout_s=60)).stdout == ""
    # The rows say the same: one checkpoint for the call, and the call that finished is attempt 2.
    head = stack.head()
    assert database.rows("SELECT sha, kind, turn_index FROM checkpoints") == [(head, "tool", 0)]
    assert database.rows("SELECT attempt, pre_sha, post_sha FROM tool_calls") == [(2, base, head)]


async def test_retry_after_the_commit_but_before_the_result_replaces_the_branch_head(
    env: WorkflowEnvironment, stack: Stack, database: Scratch
) -> None:
    dies, history, base = await run_edit_once(env, stack, "after", database)

    check_applied_once(stack, dies, history, base)
    head = stack.head()
    assert database.rows("SELECT attempt, pre_sha, post_sha FROM tool_calls") == [(2, base, head)]
    # The first attempt committed before it died; that commit is not on the branch, whether it
    # was the same commit as the retry's or not, and the branch has exactly one for the call.
    assert [sha for (sha,) in database.rows("SELECT sha FROM checkpoints")] == [head]
