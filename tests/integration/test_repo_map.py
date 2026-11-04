"""The repo map, built in a real container and kept in a real Postgres (LH-602).

prepare_workspace builds the sandbox of a task as it does in a run, parses the repo there, keeps
the parsed structure as an approved item of the default branch, and puts the map for the task's
issue in the blob store. The tests read the item and the blob back. The fixture is `inventory`,
and its setup command is left empty: the map reads source files only, so the virtualenv that the
default setup builds from PyPI would add time and no coverage.

Every task, container and volume has a name with a random suffix, each test removes what it
made, and the database is a throwaway that the fixture drops.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from longhaul.memory.repo_map import build_repo_map
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import ActivityEnvironment

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.context.tokens import estimate_tokens
from longhaul.contracts import Budgets, RepoSettings, SandboxLimits, TaskSpec
from longhaul.db.records import MemoryRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.memory.embedder import HashEmbedder
from longhaul.memory.store import DEFAULT_BRANCH_APPROVER, PgMemoryStore
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "tests" / "fixtures" / "repos" / "inventory"
IMAGE = "longhaul-sandbox:py312"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_maptest_"
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"

REMOVE = "Inventory.remove(self, sku: str, quantity: int) -> None"
BROKEN = "src/inventory/broken.py"
TITLE = "remove() allows negative stock"
BODY = "Inventory.remove() does not check how much is on hand, so the stock can go below zero."


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
async def memory(database: str) -> AsyncIterator[PgMemoryStore]:
    engine = make_engine(database)
    try:
        sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
        yield PgMemoryStore(sessions, HashEmbedder())
    finally:
        await engine.dispose()


@dataclass
class Rig:
    source: Path
    manager: DockerSandboxManager
    checkpoints: GitCheckpointStore
    blobs: MemoryBlobStore
    memory: PgMemoryStore
    activities: WorkspaceActivities
    tasks: list[str] = field(default_factory=list)

    @property
    def repo(self) -> str:
        return f"local:{self.source}"

    def spec(self, title: str = TITLE, body: str = BODY) -> TaskSpec:
        """A new task on the repo. Each has its own id, so that two can exist at once."""
        task_id = f"lh-map{uuid.uuid4().hex[:8]}-42"
        self.tasks.append(task_id)
        return TaskSpec(
            task_id=task_id,
            repo=self.repo,
            issue_number=42,
            title=title,
            body=body,
            # No setup: the map reads source files and needs no virtualenv.
            settings=RepoSettings(repo=self.repo, setup_command="", budgets=Budgets(task_turns=10)),
        )

    async def prepare(self, spec: TaskSpec | None = None) -> acts.PrepareWorkspaceOutput:
        out: acts.PrepareWorkspaceOutput = await ActivityEnvironment().run(
            self.activities.prepare_workspace, spec or self.spec()
        )
        return out

    async def map_of(self, out: acts.PrepareWorkspaceOutput) -> str:
        assert out.repo_map_ref is not None
        return (await self.blobs.get(out.repo_map_ref)).decode()


@pytest.fixture
async def rig(tmp_path: Path, memory: PgMemoryStore) -> AsyncIterator[Rig]:
    source = shutil.copytree(INVENTORY, tmp_path / "inventory")
    manager = DockerSandboxManager()
    blobs = MemoryBlobStore()
    checkpoints = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    activities = WorkspaceActivities(
        manager,
        checkpoints,
        MemoryRecorder(),
        image=IMAGE,
        limits=SandboxLimits(),
        memory=memory,
        blobs=blobs,
    )
    made = Rig(source, manager, checkpoints, blobs, memory, activities)
    try:
        yield made
    finally:
        for task_id in made.tasks:
            await manager.cleanup_task(task_id)
            await checkpoints.cleanup_task(task_id)


def modules_of(text: str) -> list[str]:
    """The files of a map in the order it lists them. Their lines are the ones not indented."""
    return [line.split()[0] for line in text.splitlines() if line.startswith(("src/", "tests/"))]


async def test_inventory_map(rig: Rig) -> None:
    out = await rig.prepare()

    text = await rig.map_of(out)
    # The method the issue is about, with its signature and the first line of its docstring.
    assert REMOVE in text
    assert "Decrease the stock of sku by quantity." in text
    assert "src/inventory/store.py" in text
    assert estimate_tokens(text) < 3000

    # The same text comes out of building the map in the task's own sandbox.
    sandbox = await rig.manager.get(out.sandbox_id)
    assert sandbox is not None
    assert await build_repo_map(sandbox, f"{TITLE}\n\n{BODY}") == text

    # Parsing the repo writes nothing into it.
    status = await sandbox.exec("git status --porcelain", timeout_s=60)
    assert status.stdout.strip() == ""

    # What is kept for the next task is the structure, as an approved item of the base commit.
    [item] = await rig.memory.list_items(rig.repo, kind="repo_map")
    assert (item.status, item.source) == ("approved", "default_branch")
    assert (item.source_ref, item.approved_by) == (out.base_sha, DEFAULT_BRANCH_APPROVER)
    kept = json.loads(item.content)
    assert kept["sha"] == out.base_sha
    assert "src/inventory/store.py" in [module["path"] for module in kept["modules"]]


async def test_cached_by_sha(rig: Rig) -> None:
    first = await rig.prepare()
    maps = rig.activities.repo_maps
    assert maps is not None
    assert maps.script_runs == 1

    # A second task on the same base commit reads the structure from memory. Its issue is
    # about the tests, so its map ranks them first, which the first task's map does not.
    second = await rig.prepare(rig.spec("test_store fails", "Look at tests/test_store.py."))
    assert second.base_sha == first.base_sha
    assert maps.script_runs == 1
    [item] = await rig.memory.list_items(rig.repo, kind="repo_map")
    assert item.source_ref == first.base_sha
    assert modules_of(await rig.map_of(first))[0] == "src/inventory/store.py"
    assert modules_of(await rig.map_of(second))[0] == "tests/test_store.py"

    # A new commit on the default branch is a new key: the script runs again and the map shows
    # the file that was added.
    errors = rig.source / "src" / "inventory" / "errors.py"
    errors.write_text('class InventoryError(ValueError):\n    """Bad stock."""\n')
    third = await rig.prepare()
    assert third.base_sha != first.base_sha
    assert maps.script_runs == 2
    assert "InventoryError" in await rig.map_of(third)
    assert "InventoryError" not in await rig.map_of(first)
    items = await rig.memory.list_items(rig.repo, kind="repo_map")
    assert sorted(i.source_ref or "" for i in items) == sorted([first.base_sha, third.base_sha])


async def test_syntax_error_skipped(rig: Rig, caplog: pytest.LogCaptureFixture) -> None:
    (rig.source / BROKEN).write_text("def remove(:\n    pass\n")
    caplog.set_level(logging.WARNING, logger="longhaul.memory.repo_map")

    out = await rig.prepare()

    # The rest of the map is intact, and the agent is told which file was left out and why.
    text = await rig.map_of(out)
    assert REMOVE in text
    assert [line for line in text.splitlines() if BROKEN in line] == [
        f"Not parsed: {BROKEN} (syntax error at line 1)"
    ]
    # The skip is logged, and the structure that is kept lists the file.
    assert any(
        BROKEN in record.getMessage() and record.levelno == logging.WARNING
        for record in caplog.records
    )
    entry = await rig.memory.get_verified(rig.repo, "repo_map", out.base_sha)
    assert entry is not None
    skipped = json.loads(entry.content)["skipped"]
    assert [item["path"] for item in skipped] == [BROKEN]
