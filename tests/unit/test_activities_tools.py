"""The tool_exec and append_results activities.

tool_exec runs one call in a sandbox, stores what came out and writes the tool_calls row. It
starts every call from `pre_sha` and saves the changes a call made as a checkpoint (LH-206).
append_results answers the tool_use blocks of the last assistant message with the results.
Both run inside temporalio's ActivityEnvironment, with a fake sandbox, the memory blob store and
the memory recorder.

Most tests here are about the tools and use NoCheckpoints, which stores nothing, in a folder with
no repository. The tests under "checkpoints" use a real GitCheckpointStore and a real bare repo,
with the fake sandbox standing in for the container.
"""

import asyncio
import dataclasses
import importlib.util
import os
import shlex
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.agent.rules import BAD_FINISH, SKIPPED_AFTER_FINISH
from longhaul.agent.turns import CONTROL_RESULT, validate_pairs
from longhaul.blobs.base import blob_key, make_ref
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import (
    BlobRef,
    ExecResult,
    Message,
    Sandbox,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.db.records import CheckpointRecord, MemoryRecorder, ToolCallRecord
from longhaul.durable import acts
from longhaul.durable.activities.tools import (
    SHELL_TOOLS,
    NoCheckpoints,
    ToolActivities,
    WorkspaceCheckpoints,
)
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
TASK = "lh-acme-calc-42"
BRANCH = "longhaul/42"
SHA = "a" * 40
MODULE = "src/calc/__init__.py"
FIRST = Message(role="user", content=[TextBlock(text="go")])

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git


class Lookup:
    """The part of a sandbox manager that tool_exec uses: find a sandbox by its id."""

    def __init__(self, sandbox: Sandbox | None) -> None:
        self.sandbox = sandbox

    async def get(self, sandbox_id: str) -> Sandbox | None:
        if self.sandbox is not None and self.sandbox.id == sandbox_id:
            return self.sandbox
        return None


@pytest.fixture
async def sandbox() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox("sbx-1")
    for f in sorted(CALC.rglob("*")):
        parts = set(f.relative_to(CALC).parts)
        if f.is_file() and not parts & {"REFERENCE.patch", "__pycache__", ".pytest_cache"}:
            await box.write_file(str(f.relative_to(CALC)), f.read_bytes())
    yield box
    await box.destroy()


class Rig:
    def __init__(
        self,
        sandbox: Sandbox | None,
        *,
        checkpoints: WorkspaceCheckpoints | None = None,
        registry: ToolRegistry | None = None,
        recorder: MemoryRecorder | None = None,
    ) -> None:
        self.blobs = MemoryBlobStore()
        self.recorder = recorder or MemoryRecorder()
        self.lookup = Lookup(sandbox)
        self.activities = ToolActivities(
            registry or default_registry(test_command="pytest -q"),
            self.lookup,
            self.blobs,
            self.recorder,
            checkpoints=checkpoints or NoCheckpoints(),
        )
        self.beats: list[Any] = []

    def environment(self, attempt: int = 1, heartbeat_timeout_s: float | None = None) -> Any:
        env = ActivityEnvironment()
        timeout = None if heartbeat_timeout_s is None else timedelta(seconds=heartbeat_timeout_s)
        env.info = dataclasses.replace(env.info, attempt=attempt, heartbeat_timeout=timeout)
        env.on_heartbeat = lambda *details: self.beats.append(details)
        return env

    async def exec(self, inp: acts.ToolExecInput, **kwargs: Any) -> Any:
        return await self.environment(**kwargs).run(self.activities.tool_exec, inp)

    async def append(self, inp: acts.AppendResultsInput) -> Any:
        return await self.environment().run(self.activities.append_results, inp)

    async def put_text(self, name: str, text: str, turn: int = 0) -> BlobRef:
        return await self.blobs.put(text.encode(), key=blob_key(TASK, "turns", turn, name))

    async def put_json(self, name: str, model: Any, turn: int = 0) -> BlobRef:
        return await self.blobs.put_json(model, blob_key(TASK, "turns", turn, name))


def exec_input(
    name: str,
    call_id: str = "toolu_1",
    turn: int = 2,
    *,
    pre_sha: str | None = SHA,
    **arguments: Any,
) -> Any:
    return acts.ToolExecInput(
        task_id=TASK,
        turn_index=turn,
        call=acts.ToolCallSummary(id=call_id, name=name, input=arguments),
        pre_sha=pre_sha,
        sandbox_id="sbx-1",
    )


# ---------- tool_exec ----------
async def test_a_call_runs_in_the_sandbox_and_its_text_is_stored_whole(
    sandbox: FakeSandbox,
) -> None:
    rig = Rig(sandbox)
    result = await rig.exec(exec_input("read_file", path="src/calc/__init__.py"))

    assert result.ok
    assert result.call_id == "toolu_1"
    assert result.output_ref is not None
    stored = (await rig.blobs.get(result.output_ref)).decode()
    assert "return a - b" in stored
    # A short text is its own preview.
    assert result.output == stored
    assert result.output_ref.key == f"tasks/{TASK}/turns/0002/tool-toolu_1-output.txt"


async def test_a_long_text_is_cut_in_the_result_and_whole_in_the_blob(
    sandbox: FakeSandbox,
) -> None:
    rig = Rig(sandbox)
    command = f"{shlex.quote(sys.executable)} -c \"print('y' * 6000)\""
    result = await rig.exec(exec_input("run_command", command=command))

    assert result.output_ref is not None
    stored = (await rig.blobs.get(result.output_ref)).decode()
    assert stored.count("y") == 6000
    assert result.output.count("y") <= acts.OUTPUT_PREVIEW_CHARS
    assert result.output.startswith(stored[: acts.OUTPUT_PREVIEW_CHARS])
    # The preview says that it is one and where the rest is.
    assert result.output_ref.key in result.output
    assert len(result.output) < acts.OUTPUT_PREVIEW_CHARS + 300


async def test_the_result_carries_the_checkpoint_it_started_from_and_no_new_one(
    sandbox: FakeSandbox,
) -> None:
    # With NoCheckpoints a call neither moves the head nor leaves a checkpoint. The tests under
    # "checkpoints" below show what the real store does.
    rig = Rig(sandbox)
    result = await rig.exec(
        exec_input("edit_file", path="src/calc/__init__.py", old_str="a - b", new_str="a + b")
    )
    assert result.ok
    assert (result.pre_sha, result.post_sha) == (SHA, None)


async def test_a_call_the_model_got_wrong_is_a_result_and_not_an_error(
    sandbox: FakeSandbox,
) -> None:
    rig = Rig(sandbox)
    result = await rig.exec(exec_input("read_file", path="missing.py"))
    assert not result.ok
    assert result.output_ref is not None
    assert (await rig.blobs.get(result.output_ref)).decode() == result.output
    (row,) = rig.recorder.tool_calls.values()
    assert row.ok is False


async def test_an_unknown_tool_is_a_result_too(sandbox: FakeSandbox) -> None:
    result = await Rig(sandbox).exec(exec_input("no_such_tool"))
    assert not result.ok
    assert "unknown tool" in result.output


async def test_the_call_is_recorded_with_its_blobs_and_the_attempt(sandbox: FakeSandbox) -> None:
    rig = Rig(sandbox)
    result = await rig.exec(exec_input("read_file", path="src/calc/__init__.py"), attempt=2)

    (row,) = rig.recorder.tool_calls.values()
    assert (row.id, row.task_id, row.turn_index, row.name) == ("toolu_1", TASK, 2, "read_file")
    assert (row.ok, row.attempt, row.pre_sha, row.post_sha) == (True, 2, SHA, None)
    assert row.output_ref == (result.output_ref.key if result.output_ref else None)
    assert row.duration_ms == result.duration_ms
    # Arguments that arrived inline are stored too, so the row can show what was asked.
    assert row.args_ref == f"tasks/{TASK}/turns/0002/tool-toolu_1-args.json"
    args = make_ref(rig.blobs.objects[row.args_ref], row.args_ref)
    assert await rig.blobs.get_json(args, dict[str, Any]) == {"path": "src/calc/__init__.py"}


async def test_arguments_held_by_reference_are_loaded_for_the_tool(sandbox: FakeSandbox) -> None:
    rig = Rig(sandbox)
    content = "z" * 20_000
    ref = await rig.put_json("args-big.json", {"path": "big.txt", "content": content})
    inp = acts.ToolExecInput(
        task_id=TASK,
        turn_index=2,
        call=acts.ToolCallSummary(id="toolu_1", name="write_file", args_ref=ref),
        pre_sha=SHA,
        sandbox_id="sbx-1",
    )
    result = await rig.exec(inp)

    assert result.ok, result.output
    assert (await sandbox.read_file("big.txt")).decode() == content
    # The arguments already have a blob, so the row points at it.
    (row,) = rig.recorder.tool_calls.values()
    assert row.args_ref == ref.key


async def test_a_sandbox_that_is_gone_fails_the_activity_without_a_retry() -> None:
    rig = Rig(None)
    with pytest.raises(ApplicationError) as caught:
        await rig.exec(exec_input("read_file", path="a.py"))
    assert caught.value.type == "SandboxGoneError"
    assert caught.value.non_retryable
    assert "sbx-1" in str(caught.value)
    assert rig.recorder.tool_calls == {}


async def test_the_activity_heartbeats_while_a_command_runs(sandbox: FakeSandbox) -> None:
    rig = Rig(sandbox)
    result = await rig.exec(exec_input("run_command", command="sleep 0.6"), heartbeat_timeout_s=0.3)
    assert result.ok
    assert len(rig.beats) >= 4


async def test_an_input_over_the_payload_limit_fails_without_a_retry(
    sandbox: FakeSandbox,
) -> None:
    rig = Rig(sandbox)
    with pytest.raises(ApplicationError) as caught:
        await rig.exec(exec_input("write_file", path="x", content="x" * 300_000))
    assert caught.value.non_retryable
    assert caught.value.type == "PayloadTooLargeError"


async def test_the_null_checkpoint_store_stores_nothing(sandbox: FakeSandbox) -> None:
    # For tests that exercise a tool and do not care about checkpoints. A worker passes the
    # real store.
    store = NoCheckpoints()
    assert await store.commit(sandbox, "lh: edit_file turn 0 call toolu_1") is None
    assert await store.restore(sandbox, SHA) is None
    assert await store.is_at(sandbox, SHA)  # nothing to restore, so it never asks for a restore


# ---------- checkpoints (LH-206) ----------
def host_git(*args: str) -> str:
    """Git on the host, as the tests read a repo: no inherited GIT_* variables."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=env, timeout=60
    )
    return result.stdout.strip()


class Spy:
    """A checkpoint store that passes every call to a real one, keeps count, and can die.

    `dies` makes the next commit fail the way a killed worker does: "before" the commit is made,
    or "after" it was made and pushed, before the result goes back.
    """

    def __init__(self, store: GitCheckpointStore) -> None:
        self.store = store
        self.restores: list[str] = []
        self.commits: list[str] = []
        self.dies: str | None = None

    async def init_task(self, task_id: str, clone_url: str, base_sha: str) -> str:
        return await self.store.init_task(task_id, clone_url, base_sha)

    async def commit(self, sandbox: Sandbox, message: str) -> str | None:
        self.commits.append(message)
        if self.dies == "before":
            self.dies = None
            raise RuntimeError("the worker was killed")
        sha = await self.store.commit(sandbox, message)
        if self.dies == "after":
            self.dies = None
            raise RuntimeError("the worker was killed")
        return sha

    async def restore(self, sandbox: Sandbox, sha: str) -> None:
        self.restores.append(sha)
        await self.store.restore(sandbox, sha)

    async def bundle(self, task_id: str) -> BlobRef:
        return await self.store.bundle(task_id)

    async def is_at(self, sandbox: Sandbox, sha: str) -> bool:
        return await self.store.is_at(sandbox, sha)


@dataclasses.dataclass
class Repo:
    """The bare repo of a task, and a fake sandbox that is a clone of it at the base commit."""

    store: GitCheckpointStore
    box: FakeSandbox
    base_sha: str

    def bare(self, *args: str) -> str:
        return host_git(f"--git-dir={self.store.repo_path(TASK)}", *args)

    def branch_head(self) -> str:
        return self.bare("rev-parse", f"refs/heads/{BRANCH}")

    def rig(
        self,
        *,
        registry: ToolRegistry | None = None,
        spy: Spy | None = None,
        recorder: MemoryRecorder | None = None,
    ) -> Rig:
        return Rig(self.box, checkpoints=spy or self.store, registry=registry, recorder=recorder)

    async def text(self, path: str = MODULE) -> str:
        return (await self.box.read_file(path)).decode()


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Repo]:
    source = make_fixture_git(CALC, tmp_path / "source")
    base_sha = host_git("-C", str(source), "rev-parse", "HEAD")
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
    await store.init_task(TASK, str(source), base_sha)
    # The fake sandbox cannot mount anything, so it reads the repo from its host path.
    store.mount_point = str(store.repo_path(TASK))
    box = FakeSandbox("sbx-1")
    await store.attach(box, TASK)
    try:
        yield Repo(store, box, base_sha)
    finally:
        await box.destroy()


def edit(pre_sha: str | None, **fields: Any) -> Any:
    """An edit that is not idempotent: run twice on the same file it adds the suffix twice."""
    arguments = {"path": MODULE, "old_str": "return a - b", "new_str": "return a - b + 1"}
    return exec_input("edit_file", pre_sha=pre_sha, **{**arguments, **fields})


async def test_a_call_that_changes_files_is_saved_as_a_checkpoint(repo: Repo) -> None:
    rig = repo.rig()
    result = await rig.exec(edit(repo.base_sha))

    assert result.ok
    assert result.pre_sha == repo.base_sha
    assert result.post_sha is not None and result.post_sha != repo.base_sha
    # The commit is the edit on top of pre_sha, and it is the head of the checkpoint branch.
    assert repo.branch_head() == result.post_sha
    assert repo.bare("rev-parse", f"{result.post_sha}^") == repo.base_sha
    assert repo.bare("log", "-1", "--format=%s", result.post_sha) == (
        "lh: edit_file turn 2 call toolu_1"
    )
    assert "return a - b + 1" in repo.bare("show", f"{result.post_sha}:{MODULE}")
    # The row of the call and the row of the checkpoint name the same commit.
    (row,) = rig.recorder.tool_calls.values()
    assert (row.pre_sha, row.post_sha) == (repo.base_sha, result.post_sha)
    assert rig.recorder.checkpoints == [CheckpointRecord(TASK, result.post_sha, "tool", 2)]


async def test_a_call_that_only_reads_leaves_no_checkpoint(repo: Repo) -> None:
    rig = repo.rig()
    result = await rig.exec(exec_input("read_file", pre_sha=repo.base_sha, path=MODULE))

    assert result.ok
    assert (result.pre_sha, result.post_sha) == (repo.base_sha, None)
    assert repo.branch_head() == repo.base_sha
    assert rig.recorder.checkpoints == []


async def test_a_call_that_fails_to_change_a_file_leaves_no_checkpoint(repo: Repo) -> None:
    rig = repo.rig()
    result = await rig.exec(edit(repo.base_sha, old_str="no such text"))

    assert not result.ok
    assert result.post_sha is None
    assert repo.branch_head() == repo.base_sha
    assert rig.recorder.checkpoints == []


async def test_each_call_starts_from_the_checkpoint_the_one_before_left(repo: Repo) -> None:
    rig = repo.rig()
    first = await rig.exec(edit(repo.base_sha))
    second = await rig.exec(
        exec_input(
            "write_file", "toolu_2", 3, pre_sha=first.post_sha, path="NOTES.md", content="hi\n"
        )
    )

    assert first.post_sha is not None and second.post_sha is not None
    assert repo.bare("rev-parse", f"{second.post_sha}^") == first.post_sha
    assert repo.branch_head() == second.post_sha
    assert repo.bare("rev-list", "--count", f"refs/heads/{BRANCH}") == "3"
    assert [(c.sha, c.turn_index) for c in rig.recorder.checkpoints] == [
        (first.post_sha, 2),
        (second.post_sha, 3),
    ]


async def test_a_command_that_changes_files_is_saved_and_one_that_does_not_is_not(
    repo: Repo,
) -> None:
    # The tool cannot say in advance whether a command writes, so the tree is looked at after it.
    rig = repo.rig()
    wrote = await rig.exec(
        exec_input("run_command", pre_sha=repo.base_sha, command="echo built > out.txt")
    )
    read = await rig.exec(
        exec_input("run_command", "toolu_2", pre_sha=wrote.post_sha, command="cat out.txt")
    )

    assert wrote.post_sha is not None
    assert repo.bare("show", f"{wrote.post_sha}:out.txt") == "built"
    assert repo.bare("log", "-1", "--format=%s", wrote.post_sha) == (
        "lh: run_command turn 2 call toolu_1"
    )
    assert read.post_sha is None
    assert repo.branch_head() == wrote.post_sha


async def test_a_test_run_that_writes_files_is_saved_and_a_clean_one_is_not(repo: Repo) -> None:
    dirty = repo.rig(registry=default_registry(test_command="echo report > report.txt"))
    wrote = await dirty.exec(exec_input("run_tests", pre_sha=repo.base_sha))
    clean = repo.rig(registry=default_registry(test_command="true"))
    quiet = await clean.exec(exec_input("run_tests", "toolu_2", pre_sha=wrote.post_sha))

    assert wrote.post_sha is not None
    assert repo.bare("show", f"{wrote.post_sha}:report.txt") == "report"
    assert quiet.post_sha is None
    assert clean.recorder.checkpoints == []


async def test_files_that_git_ignores_do_not_make_a_checkpoint(repo: Repo) -> None:
    command = "mkdir -p .venv/bin src/calc/__pycache__ && echo x > src/calc/__pycache__/m.pyc"
    result = await repo.rig().exec(
        exec_input("run_command", pre_sha=repo.base_sha, command=command)
    )
    assert result.ok and result.post_sha is None


async def test_a_call_starts_from_pre_sha_so_a_half_applied_edit_is_gone(repo: Repo) -> None:
    # What an attempt that died after its tool ran leaves behind: a changed file and a new one.
    await repo.box.write_file(MODULE, b"half applied\n")
    await repo.box.write_file("leftover.txt", b"x\n")

    result = await repo.rig().exec(
        exec_input("read_file", pre_sha=repo.base_sha, path=MODULE), attempt=2
    )

    assert "half applied" not in result.output
    assert "return a - b" in result.output
    assert not (repo.box.root / "leftover.txt").exists()


async def test_a_call_starts_from_pre_sha_when_the_workspace_is_on_another_commit(
    repo: Repo,
) -> None:
    rig = repo.rig()
    moved = await rig.exec(edit(repo.base_sha))
    assert moved.post_sha is not None

    # The workflow goes back to an earlier head: the workspace is clean, but on a later commit.
    result = await rig.exec(exec_input("read_file", "toolu_2", pre_sha=repo.base_sha, path=MODULE))

    assert "return a - b + 1" not in result.output
    assert host_git("-C", str(repo.box.root), "rev-parse", "HEAD") == repo.base_sha


async def test_a_workspace_that_is_already_at_pre_sha_is_not_restored(repo: Repo) -> None:
    # A restore deletes ignored files, so it is kept for the calls that need it.
    spy = Spy(repo.store)
    rig = repo.rig(spy=spy)
    # The cache folder is in the fixture's .gitignore, like the output of a build would be.
    await repo.box.write_file(".pytest_cache/lastfailed", b"{}\n")
    first = await rig.exec(edit(repo.base_sha))
    assert first.post_sha is not None
    spy.restores.clear()

    await rig.exec(exec_input("read_file", "toolu_2", pre_sha=first.post_sha, path=MODULE))
    await rig.exec(exec_input("list_dir", "toolu_3", pre_sha=first.post_sha, path="."))

    assert spy.restores == []
    assert (repo.box.root / ".pytest_cache" / "lastfailed").exists()


async def test_pre_sha_is_restored_when_the_workspace_is_dirty_or_elsewhere(repo: Repo) -> None:
    spy = Spy(repo.store)
    rig = repo.rig(spy=spy)
    await repo.box.write_file("stray.txt", b"x\n")
    await rig.exec(exec_input("read_file", pre_sha=repo.base_sha, path=MODULE))
    assert spy.restores == [repo.base_sha]

    moved = await rig.exec(edit(repo.base_sha))
    spy.restores.clear()
    await rig.exec(exec_input("read_file", "toolu_2", pre_sha=repo.base_sha, path=MODULE))
    assert moved.post_sha is not None
    assert spy.restores == [repo.base_sha]


async def test_a_call_without_a_pre_sha_runs_where_it_is_and_is_still_saved(repo: Repo) -> None:
    spy = Spy(repo.store)
    result = await repo.rig(spy=spy).exec(edit(None))

    assert spy.restores == []
    assert result.pre_sha is None
    assert result.post_sha is not None and repo.branch_head() == result.post_sha


async def test_a_retry_after_the_worker_died_before_the_commit_applies_the_edit_once(
    repo: Repo,
) -> None:
    spy = Spy(repo.store)
    spy.dies = "before"
    rig = repo.rig(spy=spy)

    with pytest.raises(RuntimeError, match="killed"):
        await rig.exec(edit(repo.base_sha))
    # The edit is in the workspace and nowhere else: the first attempt died with it half done.
    assert (await repo.text()).count("+ 1") == 1
    assert repo.branch_head() == repo.base_sha
    assert rig.recorder.tool_calls == {} and rig.recorder.checkpoints == []

    result = await rig.exec(edit(repo.base_sha), attempt=2)

    assert result.ok and result.post_sha is not None
    assert (await repo.text()).count("+ 1") == 1
    assert repo.bare("rev-list", "--count", f"refs/heads/{BRANCH}") == "2"
    assert repo.bare("rev-parse", f"{result.post_sha}^") == repo.base_sha
    assert [c.sha for c in rig.recorder.checkpoints] == [result.post_sha]
    assert [row.attempt for row in rig.recorder.tool_calls.values()] == [2]


async def test_a_retry_after_the_worker_died_after_the_commit_replaces_the_branch_head(
    repo: Repo,
) -> None:
    spy = Spy(repo.store)
    spy.dies = "after"
    rig = repo.rig(spy=spy)

    with pytest.raises(RuntimeError, match="killed"):
        await rig.exec(edit(repo.base_sha))
    lost = repo.branch_head()
    assert lost != repo.base_sha  # the first attempt got as far as pushing its commit

    result = await rig.exec(edit(repo.base_sha), attempt=2)

    assert result.post_sha is not None
    assert (await repo.text()).count("+ 1") == 1
    # The chain stays a line: the branch has the base and one commit for the call, and the commit
    # that was replaced can still be restored by its sha.
    assert repo.branch_head() == result.post_sha
    assert repo.bare("rev-list", "--count", f"refs/heads/{BRANCH}") == "2"
    assert repo.bare("rev-parse", f"{result.post_sha}^") == repo.base_sha
    assert repo.bare("cat-file", "-t", lost) == "commit"


# What the agent does with git itself inside a shell command. The sandbox image sets an
# identity, and the fake sandbox has none, so the commands name one.
AGENT_GIT = "git -c user.name=agent -c user.email=agent@example.com"


async def test_a_command_that_commits_by_itself_is_saved_on_top_of_pre_sha(repo: Repo) -> None:
    # The tree is clean against the new HEAD, so only a comparison with pre_sha sees the work.
    rig = repo.rig()
    command = f"echo fixed > fix.txt && git add fix.txt && {AGENT_GIT} commit -qm 'my own commit'"
    result = await rig.exec(exec_input("run_command", pre_sha=repo.base_sha, command=command))

    assert result.ok and result.post_sha is not None
    assert repo.branch_head() == result.post_sha
    assert repo.bare("rev-parse", f"{result.post_sha}^") == repo.base_sha
    assert repo.bare("rev-list", "--count", f"refs/heads/{BRANCH}") == "2"
    assert repo.bare("show", f"{result.post_sha}:fix.txt") == "fixed"
    assert host_git("-C", str(repo.box.root), "rev-parse", "HEAD") == result.post_sha
    assert rig.recorder.checkpoints == [CheckpointRecord(TASK, result.post_sha, "tool", 2)]

    # The next call starts from the returned head and still sees the work.
    read = await rig.exec(
        exec_input("read_file", "toolu_2", pre_sha=result.post_sha, path="fix.txt")
    )
    assert "fixed" in read.output


async def test_a_command_that_commits_a_change_to_a_tracked_file_keeps_it(repo: Repo) -> None:
    command = f"echo '# note' >> {MODULE} && {AGENT_GIT} commit -qam 'fix add'"
    rig = repo.rig()
    result = await rig.exec(exec_input("run_command", pre_sha=repo.base_sha, command=command))
    assert result.post_sha is not None

    read = await rig.exec(exec_input("read_file", "toolu_2", pre_sha=result.post_sha, path=MODULE))
    assert "# note" in read.output


@pytest.mark.parametrize(
    "command",
    [f"{AGENT_GIT} revert --no-edit HEAD", "git reset -q --hard HEAD~1"],
    ids=["revert", "reset"],
)
async def test_a_command_that_undoes_the_last_checkpoint_is_saved_as_a_new_one(
    repo: Repo, command: str
) -> None:
    rig = repo.rig()
    first = await rig.exec(edit(repo.base_sha))
    assert first.post_sha is not None

    undone = await rig.exec(
        exec_input("run_command", "toolu_2", 3, pre_sha=first.post_sha, command=command)
    )

    # The chain stays a line that only grows: base, the edit, and a commit that takes it back.
    assert undone.post_sha is not None and undone.post_sha != first.post_sha
    assert repo.bare("rev-parse", f"{undone.post_sha}^") == first.post_sha
    assert repo.branch_head() == undone.post_sha
    assert repo.bare("rev-parse", f"{undone.post_sha}^{{tree}}") == repo.bare(
        "rev-parse", f"{repo.base_sha}^{{tree}}"
    )
    read = await rig.exec(
        exec_input("read_file", "toolu_3", 4, pre_sha=undone.post_sha, path=MODULE)
    )
    assert "return a - b + 1" not in read.output


async def test_a_command_that_commits_nothing_new_leaves_the_workspace_at_pre_sha(
    repo: Repo,
) -> None:
    spy = Spy(repo.store)
    rig = repo.rig(spy=spy)
    # An empty commit moves HEAD and changes no file.
    command = f"{AGENT_GIT} commit -q --allow-empty -m 'nothing'"
    result = await rig.exec(exec_input("run_command", pre_sha=repo.base_sha, command=command))

    assert result.ok and result.post_sha is None
    assert repo.branch_head() == repo.base_sha
    assert rig.recorder.checkpoints == []
    # HEAD is back on pre_sha, so the next call has nothing to restore.
    assert await repo.store.is_at(repo.box, repo.base_sha)
    await rig.exec(exec_input("read_file", "toolu_2", pre_sha=repo.base_sha, path=MODULE))
    assert spy.restores == []


class KilledRecorder(MemoryRecorder):
    """A recorder whose worker dies once, in one write: before the row is stored or after it."""

    def __init__(self, write: str, *, after: bool) -> None:
        super().__init__()
        self.write, self.after, self.armed = write, after, True

    async def _killed(self, name: str, store: Any, record: Any) -> None:
        dies = self.armed and name == self.write
        if dies and not self.after:
            self.armed = False
            raise RuntimeError("the worker was killed")
        await store(record)
        if dies and self.after:
            self.armed = False
            raise RuntimeError("the worker was killed")

    async def record_tool_call(self, record: ToolCallRecord) -> None:
        await self._killed("record_tool_call", super().record_tool_call, record)

    async def record_checkpoint(self, record: CheckpointRecord) -> None:
        await self._killed("record_checkpoint", super().record_checkpoint, record)


async def test_a_retry_after_the_worker_died_before_the_call_row_leaves_one_checkpoint_row(
    repo: Repo,
) -> None:
    recorder = KilledRecorder("record_tool_call", after=False)
    rig = repo.rig(recorder=recorder)

    with pytest.raises(RuntimeError, match="killed"):
        await rig.exec(edit(repo.base_sha))
    lost = repo.branch_head()
    assert lost != repo.base_sha  # the first attempt pushed its commit
    await asyncio.sleep(1.1)  # a commit made a second later has another sha

    result = await rig.exec(edit(repo.base_sha), attempt=2)

    assert result.post_sha is not None and result.post_sha != lost
    # A checkpoint row names a commit that is on the branch: the first attempt wrote none for
    # `lost`, because its call row did not get stored.
    assert recorder.checkpoints == [CheckpointRecord(TASK, result.post_sha, "tool", 2)]
    (row,) = recorder.tool_calls.values()
    assert row.post_sha == result.post_sha
    assert repo.branch_head() == result.post_sha
    assert repo.bare("rev-list", "--count", f"refs/heads/{BRANCH}") == "2"


async def test_a_retry_after_the_checkpoint_row_was_stored_keeps_the_old_row(repo: Repo) -> None:
    # The one window that is left: the worker dies after its last write, before Temporal gets the
    # result. The row of the replaced commit stays, and the commit stays restorable, because a
    # checkpoint row means "this commit was saved", not "this commit is on the branch". The call
    # row and the result name the commit that counts.
    recorder = KilledRecorder("record_checkpoint", after=True)
    rig = repo.rig(recorder=recorder)

    with pytest.raises(RuntimeError, match="killed"):
        await rig.exec(edit(repo.base_sha))
    lost = repo.branch_head()
    await asyncio.sleep(1.1)

    result = await rig.exec(edit(repo.base_sha), attempt=2)

    assert result.post_sha is not None and result.post_sha != lost
    assert [c.sha for c in recorder.checkpoints] == [lost, result.post_sha]
    assert repo.bare("cat-file", "-t", lost) == "commit"
    (row,) = recorder.tool_calls.values()
    assert row.post_sha == result.post_sha == repo.branch_head()
    assert (await repo.text()).count("+ 1") == 1
    assert repo.bare("rev-list", "--count", f"refs/heads/{BRANCH}") == "2"


async def test_an_odd_call_id_makes_a_safe_commit_message(repo: Repo) -> None:
    result = await repo.rig().exec(
        exec_input(
            "write_file", "call:1/odd id", pre_sha=repo.base_sha, path="a.txt", content="a\n"
        )
    )

    assert result.post_sha is not None
    message = repo.bare("log", "-1", "--format=%s", result.post_sha)
    assert message.startswith("lh: write_file turn 2 call ")
    assert message.rsplit(" ", 1)[1].replace("-", "").replace("_", "").isalnum()


# ---------- a sandbox that goes away during a call ----------
class Dying(FakeSandbox):
    """A sandbox whose container disappears during the first command, as `docker rm -f` does."""

    def __init__(self, sandbox_id: str, lookup: Lookup, error: Exception) -> None:
        super().__init__(sandbox_id)
        self.lookup, self.error = lookup, error

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        self.lookup.sandbox = None
        raise self.error


async def test_a_sandbox_that_vanishes_during_a_call_is_reported_as_gone(repo: Repo) -> None:
    rig = Rig(None, checkpoints=repo.store)
    dying = Dying("sbx-1", rig.lookup, RuntimeError("No such container: sbx-1"))
    rig.lookup.sandbox = dying
    try:
        with pytest.raises(ApplicationError) as caught:
            await rig.exec(exec_input("run_command", pre_sha=repo.base_sha, command="make"))
    finally:
        await dying.destroy()

    assert caught.value.type == acts.SANDBOX_GONE
    assert caught.value.non_retryable
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert rig.recorder.tool_calls == {}


async def test_a_failure_with_the_sandbox_still_there_is_left_to_the_retry_policy(
    repo: Repo,
) -> None:
    class Broken(FakeSandbox):
        async def exec(
            self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
        ) -> ExecResult:
            raise RuntimeError("the daemon hiccuped")

    broken = Broken("sbx-1")
    rig = Rig(broken, checkpoints=repo.store)
    try:
        with pytest.raises(RuntimeError, match="hiccuped"):
            await rig.exec(exec_input("run_command", pre_sha=repo.base_sha, command="make"))
    finally:
        await broken.destroy()


async def test_an_error_stays_the_error_when_the_lookup_cannot_say_if_the_sandbox_is_there(
    repo: Repo,
) -> None:
    class BrokenLookup(Lookup):
        async def get(self, sandbox_id: str) -> Sandbox | None:
            if self.sandbox is None:
                raise ConnectionError("the daemon is not answering")
            sandbox, self.sandbox = self.sandbox, None  # present at the start, then it fails
            return sandbox

    dying = Dying("sbx-1", Lookup(None), RuntimeError("boom"))
    rig = Rig(None, checkpoints=repo.store)
    rig.activities._sandboxes = BrokenLookup(dying)  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match="boom"):
            await rig.exec(exec_input("run_command", pre_sha=repo.base_sha, command="make"))
    finally:
        await dying.destroy()


def test_the_mutating_flags_decide_which_calls_are_saved() -> None:
    # tool_exec saves a checkpoint after a tool that says it mutates, and after the two tools
    # that run shell commands, whose effect on the tree is not known in advance.
    registry = default_registry()
    flags = {spec.name: registry.get(spec.name) for spec in registry.specs()}
    mutating = {name for name, tool in flags.items() if tool is not None and tool.mutates}
    assert mutating == {"edit_file", "write_file"}
    shell = set(SHELL_TOOLS)
    assert shell == {"run_command", "run_tests"}
    assert shell <= set(flags)


# ---------- append_results ----------
def calls_transcript(*blocks: ToolUseBlock) -> list[Message]:
    return [FIRST, Message(role="assistant", content=list(blocks))]


def call_block(call_id: str, name: str = "read_file", **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name=name, input=args)


def append_input(transcript_ref: BlobRef, *results: acts.ToolResultSummary, **fields: Any) -> Any:
    return acts.AppendResultsInput(
        task_id=TASK, turn_index=2, transcript_ref=transcript_ref, results=list(results), **fields
    )


def result_blocks(message: Message) -> list[ToolResultBlock]:
    return [b for b in message.content if isinstance(b, ToolResultBlock)]


async def test_results_answer_the_calls_in_call_order() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a"), call_block("b")))
    out_ref = await rig.append(
        append_input(
            ref,
            # The results arrive in another order than the calls.
            acts.ToolResultSummary(call_id="b", ok=True, output="second"),
            acts.ToolResultSummary(call_id="a", ok=False, output="first"),
        )
    )

    transcript = await rig.blobs.get_json(out_ref, list[Message])
    first, second = result_blocks(transcript[-1])
    assert (first.tool_use_id, first.content, first.is_error) == ("a", "first", True)
    assert (second.tool_use_id, second.content, second.is_error) == ("b", "second", False)
    validate_pairs(transcript)
    assert out_ref.key == f"tasks/{TASK}/turns/0002/transcript-next.json"


async def test_a_text_held_by_reference_is_read_from_the_blob() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a")))
    text = "line\n" * 5000
    stored = await rig.put_text("tool-a-output.txt", text)
    out_ref = await rig.append(
        append_input(ref, acts.ToolResultSummary(call_id="a", ok=True, output_ref=stored))
    )
    (answer,) = result_blocks((await rig.blobs.get_json(out_ref, list[Message]))[-1])
    assert answer.content == text


async def test_a_good_finish_gets_ok_and_the_calls_after_it_say_they_did_not_run() -> None:
    rig = Rig(None)
    transcript = calls_transcript(
        call_block("a"),
        call_block("f", "finish", summary="Done."),
        call_block("c", "edit_file", path="x"),
    )
    ref = await rig.put_json("t.json", transcript)
    out_ref = await rig.append(
        append_input(ref, acts.ToolResultSummary(call_id="a", ok=True, output="contents"))
    )

    ran, ended, skipped = result_blocks((await rig.blobs.get_json(out_ref, list[Message]))[-1])
    assert (ran.content, ran.is_error) == ("contents", False)
    assert (ended.content, ended.is_error) == (CONTROL_RESULT, False)
    assert skipped.is_error
    assert skipped.content == f"not run: {SKIPPED_AFTER_FINISH}"


async def test_a_finish_without_a_summary_is_answered_with_the_error_text() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("f", "finish")))
    out_ref = await rig.append(
        append_input(ref, acts.ToolResultSummary(call_id="f", ok=False, output=BAD_FINISH))
    )
    (answer,) = result_blocks((await rig.blobs.get_json(out_ref, list[Message]))[-1])
    assert (answer.content, answer.is_error) == (BAD_FINISH, True)


async def test_text_from_the_harness_goes_after_the_results() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a")))
    out_ref = await rig.append(
        append_input(
            ref,
            acts.ToolResultSummary(call_id="a", ok=True, output="fine"),
            user_text="Use tools or call finish.",
        )
    )
    last = (await rig.blobs.get_json(out_ref, list[Message]))[-1]
    assert isinstance(last.content[0], ToolResultBlock)
    assert last.content[1] == TextBlock(text="Use tools or call finish.")


async def test_a_nudge_after_a_reply_with_no_calls_is_a_new_user_message() -> None:
    rig = Rig(None)
    transcript = [FIRST, Message(role="assistant", content=[TextBlock(text="Looks fine.")])]
    ref = await rig.put_json("t.json", transcript)
    out_ref = await rig.append(append_input(ref, user_text="Use tools or call finish."))

    stored = await rig.blobs.get_json(out_ref, list[Message])
    assert stored[:-1] == transcript
    assert stored[-1] == Message(role="user", content=[TextBlock(text="Use tools or call finish.")])


async def test_a_nudge_after_an_empty_reply_joins_the_last_user_message() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", [FIRST])
    out_ref = await rig.append(append_input(ref, user_text="Use tools or call finish."))
    stored = await rig.blobs.get_json(out_ref, list[Message])
    assert [m.role for m in stored] == ["user"]
    assert stored[0].content == [*FIRST.content, TextBlock(text="Use tools or call finish.")]


async def test_a_result_for_a_call_that_is_not_in_the_turn_fails_without_a_retry() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a")))
    with pytest.raises(ApplicationError) as caught:
        await rig.append(
            append_input(ref, acts.ToolResultSummary(call_id="zzz", ok=True, output="x"))
        )
    assert caught.value.non_retryable
    assert caught.value.type == "TranscriptError"


async def test_results_for_a_transcript_that_has_no_calls_fail_without_a_retry() -> None:
    rig = Rig(None)
    ref = await rig.put_json(
        "t.json", [FIRST, Message(role="assistant", content=[TextBlock(text="hi")])]
    )
    with pytest.raises(ApplicationError) as caught:
        await rig.append(
            append_input(ref, acts.ToolResultSummary(call_id="a", ok=True, output="x"))
        )
    assert caught.value.type == "TranscriptError"


async def test_hundreds_of_results_by_reference_fit_in_one_payload() -> None:
    rig = Rig(None)
    blocks = [call_block(f"toolu_{n:03d}", path=f"f{n}.py") for n in range(300)]
    ref = await rig.put_json("t.json", calls_transcript(*blocks))
    stored = await rig.put_text("tool-out.txt", "x" * 15_000)
    summaries = [acts.ToolResultSummary(call_id=b.id, ok=True, output_ref=stored) for b in blocks]
    out_ref = await rig.append(append_input(ref, *summaries))
    answers = result_blocks((await rig.blobs.get_json(out_ref, list[Message]))[-1])
    assert len(answers) == 300
    assert all(len(a.content) == 15_000 for a in answers)


async def test_every_input_is_guarded_by_the_payload_limit() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a")))
    with pytest.raises(ApplicationError) as caught:
        await rig.append(append_input(ref, user_text="x" * 300_000))
    assert caught.value.type == "PayloadTooLargeError"
    assert caught.value.non_retryable


async def test_activities_list_what_a_worker_registers() -> None:
    rig = Rig(None)
    names = sorted(a.__name__ for a in rig.activities.activities)
    assert names == ["append_results", "tool_exec"]
    assert asyncio.iscoroutinefunction(rig.activities.tool_exec)
