"""Git checkpoint store against real containers (LH-205).

The bare repo of each task lives under pytest's tmp_path on the host and is mounted at /ckpt in
a sandbox that DockerSandboxManager starts. Every test builds its own task, so the tests can run
in any order. Sandboxes go through cleanup_task and the bare repos are removed in a finally
block.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore, bundle_key
from longhaul.contracts import Sandbox, SandboxLimits
from longhaul.sandbox.docker_manager import DockerSandboxManager

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

IMAGE = "longhaul-sandbox:py312"
ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
MODULE = "src/calc/__init__.py"
FIXED = "def add(a: int, b: int) -> int:\n    return a + b\n"

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
