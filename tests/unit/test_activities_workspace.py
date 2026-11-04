"""The prepare_workspace and ensure_sandbox activities, on fake sandboxes (LH-206).

The sandboxes are temp folders and the checkpoint store is a real one on the host, so the git side
is real: the bare repo, the clone into the workspace, the restore. Docker is the part these tests
leave out. `Host` stands in for DockerSandboxManager and keeps what Docker keeps: containers that
come and go, a network switch for each, and a volume that outlives the container it was mounted
in. tests/integration/test_sandbox_recovery.py runs the same activities on real containers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from longhaul.telemetry.metrics import Metrics
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.agent.workspace import SetupCommandError, collect_source_files, default_setup_command
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import CheckpointError, GitCheckpointStore
from longhaul.checkpoints.snapshot import snapshot_folder
from longhaul.contracts import BlobRef, ExecResult, RepoSettings, Sandbox, SandboxLimits, TaskSpec
from longhaul.db.records import CheckpointRecord, EventRecord, MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.workspace import (
    SANDBOX_RECREATED,
    SETUP_TIMEOUT_S,
    WorkspaceActivities,
)
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.telemetry import metrics

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
TASK = "lh-acme-calc-42"
BRANCH = "longhaul/42"
MODULE = "src/calc/__init__.py"
FIXED = "def add(a: int, b: int) -> int:\n    return a + b\n"
IMAGE = "longhaul-sandbox:test"
LIMITS = SandboxLimits(cpus=1.0, memory_mb=512, pids=64)
# The default setup builds a venv from PyPI. The fake sandbox only notes that it was asked to.
VENV = "python -m venv"
# A setup command that leaves a mark in the venv folder, which git ignores and a restore keeps.
# The mark grows by one line each time the setup runs.
SETUP = "mkdir -p .venv && echo ran >> .venv/setup-log"
# Every script of the checkpoint store defines the same git wrapper, which contains this.
STORE_SCRIPT = "core.hooksPath=/dev/null"


def host_git(*args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, env=env, timeout=60
    )
    return done.stdout.strip()


# ---------- the fakes ----------
class Box(FakeSandbox):
    """A fake sandbox that notes the commands that are not the checkpoint store's.

    `commands` holds each one with the state of its network when it ran, so a test can say that
    the setup ran with the network on.
    """

    def __init__(self, sandbox_id: str, host: Host, task_id: str) -> None:
        super().__init__(sandbox_id)
        self.host, self.task_id = host, task_id
        self.commands: list[tuple[str, bool]] = []

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        if STORE_SCRIPT not in cmd:
            self.commands.append((cmd, self.host.network[self.id]))
        if cmd.startswith(VENV):
            return ExecResult(exit_code=0, stdout="", stderr="", timed_out=False, duration_ms=0)
        return await super().exec(cmd, timeout_s=timeout_s, env=env)


class Host:
    """The part of DockerSandboxManager that the workspace activities use."""

    def __init__(self) -> None:
        self.boxes: dict[str, Box] = {}
        self.running: set[str] = set()
        self.volumes: dict[str, Path] = {}  # what the workspace volume of a task holds
        self.network: dict[str, bool] = {}
        self.created: list[dict[str, Any]] = []
        self.log: list[str] = []
        self._count = 0
        self._scratch = Path(tempfile.mkdtemp(prefix="lh-volumes-"))

    async def create(
        self,
        task_id: str,
        image: str,
        limits: SandboxLimits,
        network: bool,
        mounts: Mapping[str, str] | None = None,
    ) -> Sandbox:
        self._count += 1
        box = Box(f"sbx-{self._count}", self, task_id)
        if task_id in self.volumes:  # a new container over the volume of an old one
            shutil.copytree(self.volumes[task_id], box.root, symlinks=True, dirs_exist_ok=True)
        self.boxes[box.id] = box
        self.running.add(box.id)
        self.network[box.id] = network
        self.created.append(
            {
                "task_id": task_id,
                "image": image,
                "limits": limits,
                "network": network,
                "mounts": dict(mounts or {}),
            }
        )
        self.log.append(f"create {box.id}")
        return box

    async def get(self, sandbox_id: str) -> Sandbox | None:
        # Like the Docker manager: a container that exists and does not run is not there.
        return self.boxes[sandbox_id] if sandbox_id in self.running else None

    async def set_network(self, sandbox_id: str, enabled: bool) -> None:
        self.network[sandbox_id] = enabled
        self.log.append(f"network {sandbox_id} {'on' if enabled else 'off'}")

    async def remove_containers(self, task_id: str) -> None:
        for box in [b for b in self.boxes.values() if b.task_id == task_id]:
            await self.rm_f(box.id)
        self.log.append(f"remove containers of {task_id}")

    async def cleanup_task(self, task_id: str) -> None:
        await self.remove_containers(task_id)
        volume = self.volumes.pop(task_id, None)
        if volume is not None:
            shutil.rmtree(volume, ignore_errors=True)
        self.log.append(f"remove volume of {task_id}")

    # -- what a test does to the host, as Docker or a person would --
    async def rm_f(self, sandbox_id: str) -> None:
        """`docker rm -f`: the container goes and its volume stays."""
        box = self.boxes.pop(sandbox_id)
        self.running.discard(sandbox_id)
        volume = self._scratch / box.task_id
        shutil.rmtree(volume, ignore_errors=True)
        shutil.copytree(box.root, volume, symlinks=True)
        self.volumes[box.task_id] = volume
        await box.destroy()

    def stop(self, sandbox_id: str) -> None:
        """The container exists and does not run."""
        self.running.discard(sandbox_id)

    def live(self, task_id: str) -> list[str]:
        return sorted(i for i in self.running if self.boxes[i].task_id == task_id)

    def of(self, task_id: str) -> list[str]:
        return sorted(i for i, b in self.boxes.items() if b.task_id == task_id)

    async def close(self) -> None:
        for box in list(self.boxes.values()):
            await box.destroy()
        shutil.rmtree(self._scratch, ignore_errors=True)


@dataclasses.dataclass
class Rig:
    host: Host
    store: GitCheckpointStore
    recorder: MemoryRecorder
    activities: WorkspaceActivities
    source: Path
    beats: list[Any]

    def spec(self, **settings: Any) -> TaskSpec:
        return make_spec(f"local:{self.source}", **settings)

    def environment(self, heartbeat_timeout_s: float | None = None) -> ActivityEnvironment:
        env = ActivityEnvironment()
        timeout = None if heartbeat_timeout_s is None else timedelta(seconds=heartbeat_timeout_s)
        env.info = dataclasses.replace(env.info, heartbeat_timeout=timeout)
        env.on_heartbeat = lambda *details: self.beats.append(details)
        return env

    async def prepare(
        self, spec: TaskSpec | None = None, **kwargs: Any
    ) -> acts.PrepareWorkspaceOutput:
        env = self.environment(**kwargs)
        out: acts.PrepareWorkspaceOutput = await env.run(
            self.activities.prepare_workspace, spec or self.spec()
        )
        return out

    async def ensure(
        self, sandbox_id: str | None, head_sha: str, spec: TaskSpec | None = None, **kwargs: Any
    ) -> str:
        inp = acts.EnsureSandboxInput(
            task_id=TASK, head_sha=head_sha, sandbox_id=sandbox_id, spec=spec or self.spec()
        )
        out: str = await self.environment(**kwargs).run(self.activities.ensure_sandbox, inp)
        return out

    def bare(self, *args: str) -> str:
        return host_git(f"--git-dir={self.store.repo_path(TASK)}", *args)

    def box(self, sandbox_id: str) -> Box:
        return self.host.boxes[sandbox_id]

    async def edit_and_save(self, sandbox_id: str) -> str:
        """The fix, committed as a tool call would: the head that a recovery has to return to."""
        box = self.box(sandbox_id)
        await box.write_file(MODULE, FIXED.encode())
        head = await self.store.commit(box, "lh: edit_file turn 1 call c1")
        assert head is not None
        return head


def make_spec(repo: str, **settings: Any) -> TaskSpec:
    settings.setdefault("setup_command", SETUP)
    return TaskSpec(
        task_id=TASK,
        repo=repo,
        issue_number=42,
        title="add() subtracts",
        body="add(2, 3) returns -1",
        settings=RepoSettings(repo=repo, **settings),
    )


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    source = shutil.copytree(CALC, tmp_path / "calc")
    host = Host()
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
    # The fake sandbox cannot mount anything, so it reads the repo from its host path.
    store.mount_point = str(store.repo_path(TASK))
    recorder = MemoryRecorder()
    activities = WorkspaceActivities(host, store, recorder, image=IMAGE, limits=LIMITS)
    try:
        yield Rig(host, store, recorder, activities, source, beats=[])
    finally:
        await host.close()


async def sh(box: Sandbox, script: str) -> str:
    result = await box.exec(script, timeout_s=60)
    assert result.exit_code == 0, result.stderr
    return result.stdout.strip()


def setup_runs(box: Box) -> int:
    """How many times the setup command ran in this workspace, over all containers of it."""
    log = box.root / ".venv" / "setup-log"
    return len(log.read_text().splitlines()) if log.exists() else 0


async def until(condition: Callable[[], bool], what: str, wait_s: float = 15.0) -> None:
    async with asyncio.timeout(wait_s):
        while not condition():
            await asyncio.sleep(0.02)
    assert condition(), what


async def test_the_activities_list_what_a_worker_registers(rig: Rig) -> None:
    names = sorted(a.__name__ for a in rig.activities.activities)
    assert names == ["bundle_checkpoint", "cleanup", "ensure_sandbox", "prepare_workspace"]


def test_the_setup_has_less_time_than_the_activity_that_runs_it() -> None:
    # A setup that hangs has to fail with its own output, not as a timeout of the activity.
    assert timedelta(seconds=SETUP_TIMEOUT_S) < acts.ENSURE_SANDBOX.start_to_close
    assert timedelta(seconds=SETUP_TIMEOUT_S) < acts.PREPARE_WORKSPACE.start_to_close


# ---------- prepare_workspace ----------
async def test_a_local_folder_becomes_a_sandbox_at_a_base_commit(rig: Rig) -> None:
    out = await rig.prepare()

    assert len(out.base_sha) == 40
    assert out.head_sha == out.base_sha == rig.bare("rev-parse", f"refs/heads/{BRANCH}")
    assert out.sandbox_id == "sbx-1"
    box = rig.box(out.sandbox_id)
    # The workspace is a clone of the bare repo at the base commit, and `base` is tagged.
    assert await sh(box, "git rev-parse HEAD") == out.base_sha
    assert await sh(box, "git rev-parse refs/tags/base") == out.base_sha
    assert await sh(box, "git status --porcelain") == ""
    # The files are the ones the run-local copy rules keep: not the reference fix, not the issue.
    assert (box.root / "pyproject.toml").is_file()
    assert (box.root / MODULE).is_file()
    assert not (box.root / "REFERENCE.patch").exists()
    assert not (box.root / "ISSUE.md").exists()


async def test_the_container_is_made_with_the_configured_image_limits_and_the_repo_mount(
    rig: Rig,
) -> None:
    await rig.prepare()

    (created,) = rig.host.created
    assert created["task_id"] == TASK
    assert created["image"] == IMAGE
    assert created["limits"] == LIMITS
    assert created["network"] is True
    assert created["mounts"] == rig.store.mounts(TASK)


async def test_the_setup_runs_with_the_network_on_and_the_network_is_cut_after_it(
    rig: Rig,
) -> None:
    out = await rig.prepare()

    box = rig.box(out.sandbox_id)
    assert setup_runs(box) == 1
    assert box.commands == [(SETUP, True)]
    assert rig.host.network[out.sandbox_id] is False
    assert rig.host.log[-2:] == [f"create {out.sandbox_id}", f"network {out.sandbox_id} off"]


async def test_without_a_setup_command_the_default_is_read_from_the_workspace(rig: Rig) -> None:
    out = await rig.prepare(rig.spec(setup_command=None))

    box = rig.box(out.sandbox_id)
    # The same command that `longhaul run-local` runs for this folder, with the network on.
    assert box.commands == [(default_setup_command(CALC), True)]


async def test_the_default_setup_installs_pytest_next_to_a_project_without_a_dev_extra(
    rig: Rig,
) -> None:
    (rig.source / "pyproject.toml").write_text('[project]\nname = "calc"\nversion = "0.1.0"\n')

    out = await rig.prepare(rig.spec(setup_command=None))

    ((command, _),) = rig.box(out.sandbox_id).commands
    assert command.endswith("-e . pytest")


async def test_a_folder_without_pyproject_toml_has_no_default_setup(rig: Rig) -> None:
    (rig.source / "pyproject.toml").unlink()
    out = await rig.prepare(rig.spec(setup_command=None))
    assert rig.box(out.sandbox_id).commands == []


async def test_an_empty_setup_command_means_no_setup(rig: Rig) -> None:
    out = await rig.prepare(rig.spec(setup_command=""))
    assert rig.box(out.sandbox_id).commands == []


async def test_the_base_commit_is_the_same_on_every_attempt(rig: Rig) -> None:
    first = await rig.prepare()
    second = await rig.prepare()
    assert first.base_sha == second.base_sha
    assert rig.bare("config", "longhaul.base-sha") == first.base_sha


async def test_a_retry_after_an_attempt_that_died_halfway_leaves_one_live_container(
    rig: Rig, tmp_path: Path
) -> None:
    # What a killed attempt leaves behind: a running container with the network on and a volume
    # with half a workspace, a repo of this task from another base, and an unfinished clone.
    dead = await rig.host.create(TASK, IMAGE, LIMITS, network=True)
    await dead.write_file("half-installed.txt", b"x\n")
    stale = tmp_path / "stale-source"
    stale.mkdir()
    (stale / "old.txt").write_text("from an earlier version of the folder\n")
    stale_sha = await snapshot_folder(stale, collect_source_files(stale), tmp_path / "stale-repo")
    await rig.store.init_task(TASK, str(tmp_path / "stale-repo"), stale_sha)
    leftover = rig.store.root / f".{TASK}.deadbeef.tmp"
    leftover.mkdir()
    (leftover / "HEAD").write_text("ref: refs/heads/main\n")

    out = await rig.prepare()

    assert rig.host.live(TASK) == [out.sandbox_id]
    assert rig.host.of(TASK) == [out.sandbox_id], "the dead container was removed"
    assert out.base_sha != stale_sha
    assert out.base_sha == rig.bare("rev-parse", f"refs/heads/{BRANCH}")
    box = rig.box(out.sandbox_id)
    assert (box.root / MODULE).is_file()
    assert not (box.root / "half-installed.txt").exists()
    assert not (box.root / "old.txt").exists()
    assert not leftover.exists()


async def test_a_setup_that_fails_leaves_nothing_behind(rig: Rig) -> None:
    with pytest.raises(SetupCommandError, match=r"exit code 3[\s\S]*oops"):
        await rig.prepare(rig.spec(setup_command="echo oops >&2; exit 3"))

    assert rig.host.of(TASK) == []
    assert TASK not in rig.host.volumes
    assert not rig.store.has_repo(TASK)


async def test_a_setup_that_runs_out_of_time_fails_with_its_own_message(
    rig: Rig, tmp_path: Path
) -> None:
    quick = WorkspaceActivities(
        rig.host, rig.store, rig.recorder, image=IMAGE, limits=LIMITS, setup_timeout_s=1
    )
    env = rig.environment()
    with pytest.raises(SetupCommandError, match="timed out after 1 s"):
        await env.run(quick.prepare_workspace, rig.spec(setup_command="sleep 30"))
    assert rig.host.of(TASK) == []


async def test_a_cancelled_prepare_removes_what_it_made(rig: Rig) -> None:
    env = rig.environment(heartbeat_timeout_s=0.3)
    spec = rig.spec(setup_command="touch started && sleep 30")
    task = asyncio.ensure_future(env.run(rig.activities.prepare_workspace, spec))
    await until(
        lambda: any((b.root / "started").exists() for b in rig.host.boxes.values()), "setup start"
    )

    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)

    assert rig.host.of(TASK) == []
    assert not rig.store.has_repo(TASK)


async def test_the_activity_heartbeats_while_it_works(rig: Rig) -> None:
    await rig.prepare(rig.spec(setup_command="sleep 0.7"), heartbeat_timeout_s=0.3)
    assert len(rig.beats) >= 4


@pytest.mark.parametrize(
    ("repo", "kind", "words"),
    [
        ("acme/calc", "NotImplementedError", "LH-305"),
        ("local:/no/such/folder", "WorkspaceError", "/no/such/folder"),
        ("local:calc", "WorkspaceError", "absolute"),
        ("local:", "WorkspaceError", "absolute"),
    ],
)
async def test_a_repo_that_cannot_be_prepared_is_refused_without_a_retry_and_touches_nothing(
    rig: Rig, repo: str, kind: str, words: str
) -> None:
    with pytest.raises(ApplicationError, match=words) as caught:
        await rig.prepare(make_spec(repo))

    assert caught.value.non_retryable
    assert caught.value.type == kind
    assert rig.host.log == [] and rig.host.created == []
    assert not rig.store.root.exists() or list(rig.store.root.iterdir()) == []


async def test_a_folder_with_nothing_to_copy_is_refused_without_a_retry(
    rig: Rig, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ApplicationError, match="no files to copy") as caught:
        await rig.prepare(make_spec(f"local:{empty}"))
    assert caught.value.non_retryable and caught.value.type == "WorkspaceError"
    assert rig.host.log == []


async def test_a_task_id_that_could_leave_the_root_is_refused_without_a_retry(rig: Rig) -> None:
    spec = rig.spec().model_copy(update={"task_id": "../evil"})
    with pytest.raises(ApplicationError, match="task id") as caught:
        await rig.prepare(spec)
    assert caught.value.non_retryable and caught.value.type == "ValueError"
    assert rig.host.log == []


# ---------- ensure_sandbox ----------
async def test_a_sandbox_that_runs_is_kept(rig: Rig) -> None:
    out = await rig.prepare()

    again = await rig.ensure(out.sandbox_id, out.head_sha)

    assert again == out.sandbox_id
    assert len(rig.host.created) == 1
    assert rig.recorder.events == []
    assert [cmd for cmd, _ in rig.box(out.sandbox_id).commands] == [SETUP]


async def test_a_removed_container_is_recreated_at_head_sha(rig: Rig) -> None:
    out = await rig.prepare()
    head = await rig.edit_and_save(out.sandbox_id)
    await rig.host.rm_f(out.sandbox_id)

    new_id = await rig.ensure(out.sandbox_id, head)

    assert new_id != out.sandbox_id
    box = rig.box(new_id)
    assert await sh(box, "git rev-parse HEAD") == head
    assert (await box.read_file(MODULE)).decode() == FIXED
    assert await sh(box, "git status --porcelain") == ""
    assert rig.host.live(TASK) == rig.host.of(TASK) == [new_id]
    # The recreation is on record, with the checkpoint it returned to.
    assert SANDBOX_RECREATED == "sandbox_recreated"
    assert rig.recorder.events == [
        EventRecord(
            TASK,
            SANDBOX_RECREATED,
            {"head_sha": head, "previous_sandbox_id": out.sandbox_id, "sandbox_id": new_id},
        )
    ]


async def test_the_new_container_is_made_and_set_up_like_the_first(rig: Rig) -> None:
    out = await rig.prepare()
    await rig.host.rm_f(out.sandbox_id)

    new_id = await rig.ensure(out.sandbox_id, out.head_sha)

    # The same image, limits and repo mount, and the network on for the setup.
    first, second = rig.host.created
    assert second == first
    assert second["network"] is True
    # The setup runs again with the network on, and the network is cut after it.
    box = rig.box(new_id)
    assert box.commands == [(SETUP, True)]
    assert setup_runs(box) == 2  # once for the first container and once for this one
    assert rig.host.network[new_id] is False
    assert rig.host.log[-2:] == [f"create {new_id}", f"network {new_id} off"]


async def test_work_on_the_surviving_volume_is_reset_to_head_sha_and_the_venv_is_kept(
    rig: Rig,
) -> None:
    out = await rig.prepare()
    head = await rig.edit_and_save(out.sandbox_id)
    old = rig.box(out.sandbox_id)
    # What the dead container left on its volume: an edit that was never saved, a new file and a
    # venv that the setup built.
    await sh(old, "echo drift >> src/calc/__init__.py && echo new > stray.txt")
    await sh(old, "mkdir -p .venv/bin && echo built > .venv/bin/marker")
    await rig.host.rm_f(out.sandbox_id)

    new_id = await rig.ensure(out.sandbox_id, head)

    box = rig.box(new_id)
    assert (await box.read_file(MODULE)).decode() == FIXED
    assert not (box.root / "stray.txt").exists()
    assert await sh(box, "git status --porcelain") == ""
    assert (box.root / ".venv" / "bin" / "marker").read_text() == "built\n"


async def test_a_container_and_volume_that_are_both_gone_come_back_from_the_checkpoint(
    rig: Rig,
) -> None:
    out = await rig.prepare()
    head = await rig.edit_and_save(out.sandbox_id)
    await rig.host.cleanup_task(TASK)  # container and volume

    new_id = await rig.ensure(out.sandbox_id, head)

    box = rig.box(new_id)
    assert (await box.read_file(MODULE)).decode() == FIXED
    assert await sh(box, "git rev-parse HEAD") == head
    assert await sh(box, "git rev-parse refs/tags/base") == out.base_sha
    assert setup_runs(box) == 1, "the setup built the workspace again, on an empty volume"


async def test_head_sha_wins_over_the_branch_head(rig: Rig) -> None:
    # A failed attempt can leave a commit on the branch that the workflow never learned about.
    out = await rig.prepare()
    await rig.edit_and_save(out.sandbox_id)  # the branch moves on, the workflow stays at base
    await rig.host.cleanup_task(TASK)

    new_id = await rig.ensure(out.sandbox_id, out.base_sha)

    box = rig.box(new_id)
    assert await sh(box, "git rev-parse HEAD") == out.base_sha
    assert "return a - b" in (await box.read_file(MODULE)).decode()


async def test_a_container_that_exists_and_does_not_run_is_replaced_and_removed(rig: Rig) -> None:
    out = await rig.prepare()
    rig.host.stop(out.sandbox_id)

    new_id = await rig.ensure(out.sandbox_id, out.head_sha)

    assert new_id != out.sandbox_id
    assert rig.host.of(TASK) == [new_id], "exactly one container is left for the task"
    assert rig.host.live(TASK) == [new_id]


async def test_without_a_sandbox_id_one_is_made(rig: Rig) -> None:
    out = await rig.prepare()
    await rig.host.rm_f(out.sandbox_id)

    new_id = await rig.ensure(None, out.head_sha)

    assert rig.host.live(TASK) == [new_id]
    assert rig.recorder.events[0].payload["previous_sandbox_id"] is None


async def test_what_an_earlier_attempt_of_this_activity_left_is_removed(rig: Rig) -> None:
    # Attempt 1 made a container and died before it could return the id. Attempt 2 gets the
    # old id again, so it cannot know about the new one.
    out = await rig.prepare()
    await rig.host.rm_f(out.sandbox_id)
    orphan = await rig.host.create(TASK, IMAGE, LIMITS, network=True)

    new_id = await rig.ensure(out.sandbox_id, out.head_sha)

    assert new_id not in (out.sandbox_id, orphan.id)
    assert rig.host.of(TASK) == [new_id]


async def test_a_setup_that_fails_removes_the_new_container_and_records_no_event(
    rig: Rig,
) -> None:
    out = await rig.prepare()
    await rig.host.rm_f(out.sandbox_id)

    with pytest.raises(SetupCommandError, match="exit code 7"):
        await rig.ensure(out.sandbox_id, out.head_sha, rig.spec(setup_command="exit 7"))

    assert rig.host.of(TASK) == []
    assert rig.recorder.events == []
    assert TASK in rig.host.volumes, "the volume stays for the next attempt"


async def test_a_task_with_no_repo_on_this_host_cannot_be_recreated_and_nothing_is_removed(
    rig: Rig,
) -> None:
    out = await rig.prepare()
    rig.host.stop(out.sandbox_id)
    await rig.store.cleanup_task(TASK)

    with pytest.raises(ApplicationError, match="checkpoint repo") as caught:
        await rig.ensure(out.sandbox_id, out.head_sha)

    assert caught.value.non_retryable
    assert caught.value.type == "RepoNotFoundError"
    assert rig.host.of(TASK) == [out.sandbox_id], "the old container is not touched"
    assert rig.recorder.events == []


async def test_ensure_sandbox_heartbeats_while_it_works(rig: Rig) -> None:
    out = await rig.prepare()
    await rig.host.rm_f(out.sandbox_id)

    await rig.ensure(
        out.sandbox_id,
        out.head_sha,
        rig.spec(setup_command="sleep 0.7"),
        heartbeat_timeout_s=0.3,
    )

    assert len(rig.beats) >= 4


async def test_the_default_setup_is_read_from_the_workspace_at_head_sha(rig: Rig) -> None:
    # The agent may change pyproject.toml, and a recovery sets up what the workspace has now.
    spec = rig.spec(setup_command=None)
    out = await rig.prepare(spec)
    box = rig.box(out.sandbox_id)
    await box.write_file("pyproject.toml", b'[project]\nname = "calc"\nversion = "0.1.0"\n')
    head = await rig.store.commit(box, "lh: write_file turn 1 call c1")
    assert head is not None
    await rig.host.rm_f(out.sandbox_id)

    new_id = await rig.ensure(out.sandbox_id, head, spec)

    ((command, network_on),) = rig.box(new_id).commands
    assert command.endswith("-e . pytest") and network_on


# ---------- bundle_checkpoint (LH-207) ----------
async def bundle(rig: Rig, turn_index: int = 3, **kwargs: Any) -> BlobRef:
    inp = acts.BundleCheckpointInput(task_id=TASK, turn_index=turn_index)
    out: BlobRef = await rig.environment(**kwargs).run(rig.activities.bundle_checkpoint, inp)
    return out


async def test_bundle_checkpoint_uploads_the_branch_head_and_files_a_row_for_it(
    rig: Rig,
) -> None:
    out = await rig.prepare()
    head = await rig.edit_and_save(out.sandbox_id)

    ref = await bundle(rig, turn_index=7)

    assert ref.key == f"tasks/{TASK}/ckpt/{head}.bundle"
    assert rig.recorder.checkpoints == [CheckpointRecord(TASK, head, "bundle", 7, bundle_ref=ref)]
    assert ref.size > 0


async def test_a_bundle_made_again_after_a_retry_files_one_row(rig: Rig) -> None:
    out = await rig.prepare()
    await rig.edit_and_save(out.sandbox_id)

    first = await bundle(rig)
    again = await bundle(rig)

    assert again.key == first.key
    assert len(rig.recorder.checkpoints) == 1


async def test_a_bundle_of_a_later_head_files_another_row(rig: Rig) -> None:
    out = await rig.prepare()
    first_head = await rig.edit_and_save(out.sandbox_id)
    await bundle(rig, turn_index=3)
    box = rig.box(out.sandbox_id)
    await box.write_file("notes.txt", b"more\n")
    second_head = await rig.store.commit(box, "lh: write_file turn 4 call c2")
    assert second_head is not None

    await bundle(rig, turn_index=6)

    assert [(c.sha, c.turn_index) for c in rig.recorder.checkpoints] == [
        (first_head, 3),
        (second_head, 6),
    ]


async def test_bundle_checkpoint_heartbeats_while_it_works(rig: Rig) -> None:
    out = await rig.prepare()
    await rig.edit_and_save(out.sandbox_id)

    await bundle(rig, heartbeat_timeout_s=0.3)

    assert rig.beats, "a cancel and a dead worker are noticed through the heartbeat"


async def test_bundle_checkpoint_without_a_repo_is_not_retried(rig: Rig) -> None:
    # Failure mode F4: the host that held the repo is gone. Asking again finds no repo.
    with pytest.raises(ApplicationError, match="checkpoint repo") as caught:
        await bundle(rig)

    assert caught.value.non_retryable
    assert caught.value.type == "RepoNotFoundError"
    assert rig.recorder.checkpoints == []


async def test_a_bundle_that_is_too_large_files_no_row(rig: Rig) -> None:
    out = await rig.prepare()
    await rig.edit_and_save(out.sandbox_id)
    small = GitCheckpointStore(MemoryBlobStore(), root=rig.store.root, max_bundle_bytes=100)
    activities = WorkspaceActivities(rig.host, small, rig.recorder, image=IMAGE, limits=LIMITS)
    inp = acts.BundleCheckpointInput(task_id=TASK, turn_index=3)

    with pytest.raises(CheckpointError, match="larger than the limit"):
        await rig.environment().run(activities.bundle_checkpoint, inp)

    assert rig.recorder.checkpoints == []


# ---------- cleanup (LH-208) ----------
async def clean(rig: Rig, task_id: str = TASK, sandbox_id: str | None = None) -> None:
    inp = acts.CleanupInput(task_id=task_id, sandbox_id=sandbox_id, reason="cancelled by alice")
    await rig.environment().run(rig.activities.cleanup, inp)


async def test_cleanup_removes_the_container_the_volume_and_the_repo_of_a_task(rig: Rig) -> None:
    out = await rig.prepare()
    assert rig.host.live(TASK) and rig.store.has_repo(TASK)

    await clean(rig, sandbox_id=out.sandbox_id)

    assert rig.host.of(TASK) == []
    assert TASK not in rig.host.volumes
    assert not rig.store.has_repo(TASK)


async def test_cleanup_without_a_sandbox_id_finds_the_containers_by_their_task(rig: Rig) -> None:
    # The workflow stops before it learns the id when a task is cancelled during the preparation.
    await rig.prepare()
    await clean(rig, sandbox_id=None)
    assert rig.host.of(TASK) == []
    assert not rig.store.has_repo(TASK)


async def test_cleanup_twice_is_safe(rig: Rig) -> None:
    out = await rig.prepare()
    await clean(rig, sandbox_id=out.sandbox_id)
    await clean(rig, sandbox_id=out.sandbox_id)
    assert rig.host.of(TASK) == []


async def test_cleanup_of_a_task_that_never_existed_does_nothing(rig: Rig) -> None:
    await clean(rig, "lh-acme-calc-99")
    assert rig.host.log == [
        "remove containers of lh-acme-calc-99",
        "remove volume of lh-acme-calc-99",
    ]


async def test_cleanup_leaves_the_other_tasks_alone(rig: Rig) -> None:
    await rig.prepare()
    other = await rig.host.create("lh-acme-calc-7", IMAGE, LIMITS, network=False)

    await clean(rig)

    assert rig.host.of(TASK) == []
    assert rig.host.live("lh-acme-calc-7") == [other.id]


@pytest.mark.parametrize("task_id", ["../elsewhere", "a/b", ""])
async def test_cleanup_refuses_a_task_id_that_is_not_a_safe_name_for_good(
    rig: Rig, task_id: str
) -> None:
    # The id names a folder that gets deleted, so a bad one must never get that far.
    with pytest.raises(ApplicationError) as caught:
        await clean(rig, task_id)
    assert caught.value.non_retryable
    assert rig.host.log == []


async def test_a_failed_cleanup_is_retried_by_temporal(rig: Rig) -> None:
    async def broken(task_id: str) -> None:
        raise OSError("docker went away")

    rig.host.cleanup_task = broken  # type: ignore[method-assign]
    with pytest.raises(OSError, match="docker went away"):
        await clean(rig)


# ---------- the metrics of the workspace activities (LH-704) ----------
async def test_a_prepared_workspace_makes_the_task_active(rig: Rig, counters: Metrics) -> None:
    await rig.prepare()
    assert counters.registry.get_sample_value("longhaul_active_tasks") == 1


async def test_a_retried_prepare_does_not_make_the_task_active_twice(
    rig: Rig, counters: Metrics
) -> None:
    await rig.prepare()
    await rig.prepare()
    assert counters.registry.get_sample_value("longhaul_active_tasks") == 1


async def test_a_prepare_that_fails_leaves_the_task_inactive(rig: Rig, counters: Metrics) -> None:
    with pytest.raises(SetupCommandError):
        await rig.prepare(rig.spec(setup_command="exit 3"))
    assert counters.registry.get_sample_value("longhaul_active_tasks") == 0


async def test_a_recreated_sandbox_is_counted(rig: Rig, counters: Metrics) -> None:
    out = await rig.prepare()
    await rig.host.rm_f(out.sandbox_id)

    await rig.ensure(out.sandbox_id, out.head_sha)

    assert counters.registry.get_sample_value("longhaul_sandbox_recreated_total") == 1


async def test_a_sandbox_that_runs_is_not_counted_as_recreated(rig: Rig, counters: Metrics) -> None:
    out = await rig.prepare()
    await rig.ensure(out.sandbox_id, out.head_sha)
    assert counters.registry.get_sample_value("longhaul_sandbox_recreated_total") == 0


async def test_a_run_that_starts_on_a_new_worker_makes_its_task_active_there(rig: Rig) -> None:
    # After a hand-over to a new run, or a restart of the worker, the first thing a run does
    # is ensure_sandbox. The task is running on this worker from then on, whatever it finds.
    out = await rig.prepare()
    fresh = metrics.Metrics()
    with metrics.configured(fresh):
        await rig.ensure(out.sandbox_id, out.head_sha)
    assert fresh.registry.get_sample_value("longhaul_active_tasks") == 1


async def test_a_sandbox_that_could_not_be_rebuilt_is_not_counted(
    rig: Rig, counters: Metrics
) -> None:
    out = await rig.prepare()
    await rig.host.rm_f(out.sandbox_id)
    with pytest.raises(SetupCommandError):
        await rig.ensure(out.sandbox_id, out.head_sha, rig.spec(setup_command="exit 3"))
    assert counters.registry.get_sample_value("longhaul_sandbox_recreated_total") == 0
