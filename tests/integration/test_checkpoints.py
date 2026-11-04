"""Git checkpoint store against real containers (LH-205), and what tool_exec saves with it (LH-206).

The bare repo of each task lives under pytest's tmp_path on the host and is mounted at /ckpt in
a sandbox that DockerSandboxManager starts. Every test builds its own task, so the tests can run
in any order. Sandboxes go through cleanup_task and the bare repos are removed in a finally
block. The last test runs tool_exec on such a sandbox and reads the rows it wrote from a
throwaway Postgres database (migrated to head, then dropped).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import subprocess
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.testing import ActivityEnvironment

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore, bundle_key
from longhaul.contracts import Sandbox, SandboxLimits
from longhaul.db.records import MemoryRecorder, SqlRecorder
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.activities.tools import ToolActivities
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import EnvSettings, get_settings
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

IMAGE = "longhaul-sandbox:py312"
ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
MODULE = "src/calc/__init__.py"
FIXED = "def add(a: int, b: int) -> int:\n    return a + b\n"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_ckpttest_"
UNREACHABLE_DSN = "postgresql://nobody:nopass@127.0.0.1:1/never"

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git

# Everything a test looks at to say "the workspace is the same": the files and folders, the
# permission bits and the content hashes. The venv and .git are not part of the work.
_TREE = "find . -path ./.git -prune -o -path ./.venv -prune -o"
TREE_COMMANDS = {
    "paths": f"{_TREE} -print0 | sort -z | tr '\\0' '\\n'",
    "modes": f"{_TREE} -type f -exec stat -c '%a %n' {{}} + | sort -k2",
    "hashes": f"{_TREE} -type f -exec sha256sum {{}} + | sort -k2",
    "status": "git status --porcelain",
}


def host_git(*args: str) -> str:
    """Git on the host, as the tests read a repo: no inherited GIT_* variables."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=env, timeout=60
    )
    return result.stdout.strip()


async def start_sandbox(
    manager: DockerSandboxManager, store: GitCheckpointStore, task_id: str
) -> Sandbox:
    """What workspace preparation does: a sandbox with the bare repo at /ckpt, cloned."""
    box = await manager.create(
        task_id, IMAGE, SandboxLimits(), network=False, mounts=store.mounts(task_id)
    )
    await store.attach(box, task_id)
    return box


@dataclass
class Task:
    task_id: str
    store: GitCheckpointStore
    blobs: MemoryBlobStore
    manager: DockerSandboxManager
    sandbox: Sandbox
    base_sha: str
    tmp_path: Path

    @property
    def repo(self) -> Path:
        return self.store.repo_path(self.task_id)

    @property
    def branch(self) -> str:
        return "longhaul/42"

    def bare(self, *args: str) -> str:
        return host_git(f"--git-dir={self.repo}", *args)

    def branch_head(self) -> str:
        return self.bare("rev-parse", f"refs/heads/{self.branch}")

    async def sh(self, script: str, sandbox: Sandbox | None = None) -> str:
        result = await (sandbox or self.sandbox).exec(script, timeout_s=60)
        assert result.exit_code == 0, result.stderr
        return result.stdout

    async def tree(self, sandbox: Sandbox | None = None) -> dict[str, str]:
        return {name: await self.sh(command, sandbox) for name, command in TREE_COMMANDS.items()}

    async def start_sandbox(self, store: GitCheckpointStore | None = None) -> Sandbox:
        return await start_sandbox(self.manager, store or self.store, self.task_id)


@pytest.fixture
def manager() -> DockerSandboxManager:
    return DockerSandboxManager()


@pytest.fixture
async def task(tmp_path: Path, manager: DockerSandboxManager) -> AsyncIterator[Task]:
    # The id ends in the issue number, so the scratch branch is longhaul/42.
    task_id = f"lh-ck{uuid.uuid4().hex[:8]}-42"
    source = make_fixture_git(CALC, tmp_path / "source")
    base_sha = host_git("-C", str(source), "rev-parse", "HEAD")
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    try:
        await store.init_task(task_id, str(source), base_sha)
        sandbox = await start_sandbox(manager, store, task_id)
        yield Task(task_id, store, blobs, manager, sandbox, base_sha, tmp_path)
    finally:
        await manager.cleanup_task(task_id)
        await store.cleanup_task(task_id)


async def test_commit_and_head(task: Task) -> None:
    await task.sandbox.write_file(MODULE, FIXED.encode())

    sha = await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")

    assert sha is not None and sha != task.base_sha
    assert task.branch_head() == sha
    assert await task.store.head(task.task_id) == sha
    # The commit is the edit on top of base, and it carries the message it was given.
    assert task.bare("rev-parse", f"{sha}^") == task.base_sha
    assert task.bare("show", f"{sha}:{MODULE}") == FIXED.strip()
    assert task.bare("log", "-1", "--format=%s", sha) == "lh: edit_file turn 1 call c1"
    # The workspace branch and the checkpoint branch agree.
    assert (await task.sh("git rev-parse HEAD")).strip() == sha


async def test_commit_chain_follows_the_edits(task: Task) -> None:
    await task.sandbox.write_file(MODULE, FIXED.encode())
    first = await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")
    await task.sandbox.write_file("NOTES.md", b"second change\n")
    second = await task.store.commit(task.sandbox, "lh: write_file turn 2 call c2")

    assert first is not None and second is not None and first != second
    assert task.branch_head() == second
    assert task.bare("rev-list", "--count", f"refs/heads/{task.branch}") == "3"
    assert task.bare("rev-parse", f"{second}^") == first


async def test_restore_exact(task: Task) -> None:
    before = await task.tree()
    assert before["status"] == ""
    await task.sh("mkdir -p .venv/bin && echo keep > .venv/bin/marker")

    # A committed edit, then every other way a workspace can drift from a commit.
    await task.sandbox.write_file(MODULE, FIXED.encode())
    first = await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")
    assert first is not None
    committed = await task.tree()
    await task.sh(
        "echo drift >> src/calc/__init__.py"  # changed tracked file
        " && rm tests/test_calc.py"  # deleted tracked file
        " && echo new > notes.txt"  # untracked file
        " && mkdir -p src/calc/__pycache__ && echo x > src/calc/__pycache__/m.pyc"  # ignored
        " && mkdir -p deep/empty/dir"  # untracked folders
        " && chmod +x pyproject.toml"  # mode change
        " && git add notes.txt"  # staged
    )
    assert (await task.tree())["status"] != ""

    await task.store.restore(task.sandbox, first)

    assert await task.tree() == committed
    # And all the way back to the starting point.
    await task.store.restore(task.sandbox, task.base_sha)

    after = await task.tree()
    assert after == before
    assert after["status"] == ""
    assert (await task.sh("git rev-parse HEAD")).strip() == task.base_sha
    # The venv is the one thing a restore keeps, and the checkpoint branch is not touched.
    assert (await task.sh("cat .venv/bin/marker")).strip() == "keep"
    assert task.branch_head() == first


async def test_restore_then_commit_replaces_the_branch_head(task: Task) -> None:
    # A retry: the tool ran and committed, the result was lost, and the retry starts from
    # the commit before it. The new commit must replace the old branch head.
    await task.sandbox.write_file(MODULE, FIXED.encode())
    lost = await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")
    assert lost is not None

    await task.store.restore(task.sandbox, task.base_sha)
    # A different edit, so the new commit cannot be the same object as the lost one.
    await task.sandbox.write_file(MODULE, (FIXED + "# second try\n").encode())
    again = await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")

    assert again is not None and again != lost
    assert task.branch_head() == again
    assert task.bare("rev-parse", f"{again}^") == task.base_sha
    # The commit that was replaced is still in the repo, so a recorded sha stays restorable.
    assert task.bare("cat-file", "-t", lost) == "commit"


async def test_clean_tree_no_commit(task: Task) -> None:
    assert await task.store.commit(task.sandbox, "lh: nothing to save") is None
    assert task.branch_head() == task.base_sha
    assert task.bare("rev-list", "--count", f"refs/heads/{task.branch}") == "1"

    await task.sandbox.write_file(MODULE, FIXED.encode())
    sha = await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")
    assert sha is not None

    # Same tree again, and files that git ignores, are not changes.
    await task.sh("mkdir -p src/calc/__pycache__ .venv && echo x > src/calc/__pycache__/m.pyc")
    assert await task.store.commit(task.sandbox, "lh: nothing to save") is None
    assert task.branch_head() == sha
    assert task.bare("rev-list", "--count", f"refs/heads/{task.branch}") == "2"


async def test_restore_from_bundle(task: Task) -> None:
    await task.sandbox.write_file(MODULE, FIXED.encode())
    await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")
    await task.sandbox.write_file("NOTES.md", b"two\n")
    head = await task.store.commit(task.sandbox, "lh: write_file turn 2 call c2")
    assert head is not None
    committed = await task.tree()

    ref = await task.store.bundle(task.task_id)

    assert ref.key == bundle_key(task.task_id, head) == f"tasks/{task.task_id}/ckpt/{head}.bundle"
    bundle_file = task.tmp_path / "ckpt.bundle"
    bundle_file.write_bytes(await task.blobs.get(ref))
    host_git("bundle", "verify", str(bundle_file))
    # A bundle restored into an empty directory has the same head.
    empty = task.tmp_path / "empty"
    empty.mkdir()
    host_git("clone", "-q", str(bundle_file), str(empty))
    assert host_git("-C", str(empty), "rev-parse", "HEAD") == head

    # The host is lost: the bare repo and the sandbox are gone. Another host rebuilds both.
    await task.manager.cleanup_task(task.task_id)
    await task.store.cleanup_task(task.task_id)
    assert not task.repo.exists()
    new_host = GitCheckpointStore(task.blobs, root=task.tmp_path / "new-host")
    try:
        assert await new_host.restore_from_bundle(task.task_id, ref) == head
        assert await new_host.head(task.task_id) == head
        box = await task.start_sandbox(new_host)
        await new_host.restore(box, head)

        assert await task.tree(box) == committed
        assert (await task.sh("git rev-parse HEAD", box)).strip() == head
    finally:
        await new_host.cleanup_task(task.task_id)


async def test_new_container_over_the_old_volume_returns_to_the_checkpoint(task: Task) -> None:
    await task.sandbox.write_file(MODULE, FIXED.encode())
    sha = await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")
    assert sha is not None
    committed = await task.tree()
    await task.sh("echo drift >> pyproject.toml && echo new > notes.txt")

    # The container is removed and the volume stays, so the next one starts on a dirty tree.
    await task.sandbox.destroy()
    box = await task.start_sandbox()
    assert (await task.tree(box))["status"] != ""
    await task.store.restore(box, sha)

    assert await task.tree(box) == committed
    assert (await task.sh("git rev-parse HEAD", box)).strip() == sha


async def test_empty_volume_is_cloned_at_the_branch_head(task: Task) -> None:
    await task.sandbox.write_file(MODULE, FIXED.encode())
    sha = await task.store.commit(task.sandbox, "lh: edit_file turn 1 call c1")
    assert sha is not None
    committed = await task.tree()

    # Container and volume are both gone. A fresh pair is cloned from /ckpt at the head.
    await task.manager.cleanup_task(task.task_id)
    box = await task.start_sandbox()

    assert await task.tree(box) == committed
    assert (await task.sh("git rev-parse HEAD", box)).strip() == sha
    assert (await task.sh("git rev-parse refs/tags/base", box)).strip() == task.base_sha
    # The tag is what the git_diff tool compares with.
    assert "return a + b" in await task.sh("git diff refs/tags/base --", box)


async def test_no_clone_url_or_credentials_in_the_repo_or_the_sandbox(task: Task) -> None:
    source = str(task.tmp_path / "source")
    config = (task.repo / "config").read_text()
    assert source not in config
    assert "remote" not in config and "url" not in config.lower()

    inside = await task.sh("cat /ckpt/config; git config --list --show-origin; env")
    assert source not in inside
    assert "token" not in inside.lower() and "password" not in inside.lower()


async def test_agent_user_can_write_to_the_bind_mount(task: Task) -> None:
    result = await task.sandbox.exec(
        "id -u && touch /ckpt/objects/probe && rm /ckpt/objects/probe && echo writable",
        timeout_s=30,
    )
    assert result.stdout.split() == ["10001", "writable"], result.stderr


# ---------- what tool_exec saves (LH-206) ----------
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


async def test_a_command_that_commits_by_itself_is_checkpointed_and_kept(task: Task) -> None:
    """The image sets a git identity, so `git commit` works inside run_command.

    The commit moves HEAD and leaves the tree clean against it. The work must still reach a
    checkpoint on top of pre_sha, or the next call would restore pre_sha and undo it. A revert
    and a reset are saved the same way.
    """
    tools = ToolActivities(
        default_registry(test_command="true"),
        task.manager,
        task.blobs,
        MemoryRecorder(),
        checkpoints=task.store,
    )
    steps = [
        ("perl -pi -e 's/return a - b/return a + b/' src/calc/__init__.py && git commit -qam fix"),
        "git revert --no-edit HEAD",
        "git reset -q --hard HEAD~1",
    ]
    head = task.base_sha
    shas = [head]
    for turn, step in enumerate(steps):
        inp = acts.ToolExecInput(
            task_id=task.task_id,
            turn_index=turn,
            call=acts.ToolCallSummary(
                id=f"toolu_{turn}", name="run_command", input={"command": step}
            ),
            pre_sha=head,
            sandbox_id=task.sandbox.id,
        )
        env = ActivityEnvironment()
        env.info = dataclasses.replace(env.info, attempt=1)
        result = await env.run(tools.tool_exec, inp)
        assert result.ok and result.post_sha is not None, f"{step}: {result.output}"
        assert task.bare("rev-parse", f"{result.post_sha}^") == head
        head = result.post_sha
        shas.append(head)
        # The next call restores pre_sha first when the workspace is not on it. It is on it.
        assert (await task.sh("git rev-parse HEAD")).strip() == head
        assert await task.store.is_at(task.sandbox, head)

    fixed, reverted, reset = shas[1:]
    assert "return a + b" in task.bare("show", f"{fixed}:{MODULE}")
    assert "return a - b" in task.bare("show", f"{reverted}:{MODULE}")
    # The reset goes back to the first checkpoint, which had the fix.
    assert "return a + b" in task.bare("show", f"{reset}:{MODULE}")
    assert task.branch_head() == head
    assert task.bare("rev-list", "--count", f"refs/heads/{task.branch}") == "4"


async def test_every_mutation_checkpointed(
    task: Task, database: Scratch, recorder: SqlRecorder
) -> None:
    """Every call that changed the workspace has a post_sha, a checkpoints row and a commit.

    The calls run through tool_exec in a real container, each one from the sha the one before
    left, as the workflow passes them. A call that changes nothing, because it only reads or
    because it failed, has none of the three.
    """
    tools = ToolActivities(
        default_registry(test_command="true"),
        task.manager,
        task.blobs,
        recorder,
        checkpoints=task.store,
    )
    calls: list[tuple[str, dict[str, Any], bool]] = [
        ("read_file", {"path": MODULE}, False),
        ("edit_file", {"path": MODULE, "old_str": "return a - b", "new_str": "return a + b"}, True),
        ("edit_file", {"path": MODULE, "old_str": "no such text", "new_str": "x"}, False),
        ("write_file", {"path": "NOTES.md", "content": "notes\n"}, True),
        ("run_command", {"command": "echo built > out.txt"}, True),
        ("run_command", {"command": "cat out.txt"}, False),
        ("run_tests", {}, False),  # the test command is `true`, which leaves the tree clean
        ("git_diff", {}, False),
        ("list_dir", {"path": "."}, False),
        ("run_command", {"command": "echo more >> out.txt"}, True),
    ]
    head = task.base_sha
    saved: list[tuple[str, int, str]] = []  # (sha, turn, name) of each checkpoint
    results = []
    for turn, (name, arguments, mutates) in enumerate(calls):
        call = acts.ToolCallSummary(id=f"toolu_{turn}", name=name, input=arguments)
        inp = acts.ToolExecInput(
            task_id=task.task_id,
            turn_index=turn,
            call=call,
            pre_sha=head,
            sandbox_id=task.sandbox.id,
        )
        env = ActivityEnvironment()
        env.info = dataclasses.replace(env.info, attempt=1)
        result = await env.run(tools.tool_exec, inp)
        results.append(result)
        assert (result.post_sha is not None) == mutates, f"turn {turn}: {name}: {result.output}"
        assert result.pre_sha == head
        if result.post_sha is not None:
            head = result.post_sha
            saved.append((head, turn, name))
    assert len(saved) == 4

    # One row of kind tool for each checkpoint, in order, with the turn that made it.
    rows = database.rows("SELECT sha, kind, turn_index FROM checkpoints ORDER BY id")
    assert rows == [(sha, "tool", turn) for sha, turn, _ in saved]
    # The rows of the calls name the same shas, and only the calls that changed something do.
    posted = database.rows(
        "SELECT turn_index, pre_sha, post_sha FROM tool_calls "
        "WHERE post_sha IS NOT NULL ORDER BY turn_index"
    )
    assert [(turn, post) for turn, _, post in posted] == [(turn, sha) for sha, turn, _ in saved]
    assert database.rows("SELECT count(*) FROM tool_calls") == [(len(calls),)]
    assert database.rows("SELECT count(*) FROM tool_calls WHERE post_sha IS NULL") == [
        (len(calls) - len(saved),)
    ]

    # The commits are the line of the checkpoint branch, each one a child of the one before,
    # and each carries the message that names its call.
    parent = task.base_sha
    for sha, turn, name in saved:
        assert task.bare("rev-parse", f"{sha}^") == parent
        assert (
            task.bare("log", "-1", "--format=%s", sha)
            == f"lh: {name} turn {turn} call toolu_{turn}"
        )
        parent = sha
    assert task.branch_head() == head
    assert task.bare("rev-list", "--count", f"refs/heads/{task.branch}") == str(len(saved) + 1)
    assert task.bare("rev-list", "--merges", f"refs/heads/{task.branch}") == ""
    # And the last one holds all of the work.
    assert task.bare("show", f"{head}:out.txt") == "built\nmore"
    assert task.bare("show", f"{head}:NOTES.md") == "notes"
    assert "return a + b" in task.bare("show", f"{head}:{MODULE}")
