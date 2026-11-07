"""Command discovery in prepare_workspace, on a real container and a real Postgres (LH-603).

The calc fixture configures pytest in pyproject.toml and has no Makefile, no tox.ini and no CI
files. prepare_workspace builds its sandbox as it does for a task, finds `pytest -q`, runs it at
the base commit, and keeps it in the memory store as an approved command of the default branch.
The default setup builds a virtualenv from PyPI, so the machine needs outbound network, as the
other sandbox tests do.

Every task, container and volume has a name with a random suffix, each test removes what it
made, and the database is a throwaway that the fixture drops.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import ActivityEnvironment

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import Budgets, RepoSettings, SandboxLimits, TaskSpec
from longhaul.db.records import MemoryRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.memory.commands import verify_commands
from longhaul.memory.embedder import HashEmbedder
from longhaul.memory.store import DEFAULT_BRANCH_APPROVER, PgMemoryStore
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
IMAGE = "longhaul-sandbox:py312"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_disctest_"
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"


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
    task_id: str
    source: Path
    manager: DockerSandboxManager
    checkpoints: GitCheckpointStore
    activities: WorkspaceActivities

    @property
    def repo(self) -> str:
        return f"local:{self.source}"

    def spec(self) -> TaskSpec:
        return TaskSpec(
            task_id=self.task_id,
            repo=self.repo,
            issue_number=42,
            title="add() subtracts",
            body="`calc.add` subtracts its arguments instead of adding them.",
            # The default setup: a venv with calc and pytest.
            settings=RepoSettings(repo=self.repo, budgets=Budgets(task_turns=10)),
        )


@pytest.fixture
async def rig(tmp_path: Path, memory: PgMemoryStore) -> AsyncIterator[Rig]:
    task_id = f"lh-disc{uuid.uuid4().hex[:8]}-42"
    source = shutil.copytree(CALC, tmp_path / "calc")
    manager = DockerSandboxManager()
    checkpoints = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
    activities = WorkspaceActivities(
        manager,
        checkpoints,
        MemoryRecorder(),
        image=IMAGE,
        limits=SandboxLimits(),
        memory=memory,
    )
    try:
        yield Rig(task_id, source, manager, checkpoints, activities)
    finally:
        await manager.cleanup_task(task_id)
        await checkpoints.cleanup_task(task_id)


async def prepare(rig: Rig) -> acts.PrepareWorkspaceOutput:
    out: acts.PrepareWorkspaceOutput = await ActivityEnvironment().run(
        rig.activities.prepare_workspace, rig.spec()
    )
    return out


async def test_calc_commands(rig: Rig, memory: PgMemoryStore) -> None:
    out = await prepare(rig)

    # pytest -q is found in pyproject.toml, runs at the base commit and is kept as approved.
    [stored] = await memory.list_items(rig.repo)
    assert stored.content == "pytest -q"
    assert (stored.kind, stored.status, stored.source) == ("command", "approved", "default_branch")
    assert (stored.source_ref, stored.approved_by) == (out.base_sha, DEFAULT_BRANCH_APPROVER)

    # A task that retries the activity finds the same item and adds nothing.
    again = await prepare(rig)
    assert again.base_sha == out.base_sha
    assert [e.id for e in await memory.list_items(rig.repo)] == [stored.id]

    # A command that the repo does not support does not pass the check that decides what is
    # stored. calc has no Makefile, so `make test` prints no summary of a test run, and neither
    # does a command that does not exist.
    sandbox = await rig.manager.get(again.sandbox_id)
    assert sandbox is not None
    works = await verify_commands(sandbox, ["make test", "no-such-tool --check", "pytest -q"])
    assert works == ["pytest -q"]

    # Running the commands leaves the workspace as the base commit has it.
    status = await sandbox.exec("git status --porcelain && git rev-parse HEAD", timeout_s=60)
    assert status.stdout.strip() == again.head_sha


def test_the_fixture_is_the_one_this_test_describes() -> None:
    # The checks above hold for a repo with pytest configured and nothing else.
    assert "[tool.pytest.ini_options]" in (CALC / "pyproject.toml").read_text()
    for name in ("Makefile", "tox.ini", ".github"):
        assert not (CALC / name).exists()
