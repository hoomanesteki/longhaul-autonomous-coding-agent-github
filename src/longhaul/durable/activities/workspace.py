"""The prepare_workspace and ensure_sandbox activities: a sandbox that can come back.

prepare_workspace makes the first sandbox of a task and ensure_sandbox makes another when the
first is gone (failure mode F3). Both end the same way: a container that has the task's bare
repo at /ckpt, a /workspace that is a clone of it at one commit, the setup command run with the
network on, and then the network cut. They differ in where the commit comes from. The first
builds the base commit from the repo. The second is told the commit to return to, which is the
latest checkpoint the workflow knows (`head_sha`), and not whatever the branch in /ckpt holds,
because an attempt that died can leave a commit there that the workflow never learned about.

prepare_workspace handles `local:/abs/path` repos. The folder is not a git repo, so it is
committed first (checkpoints/snapshot.py) using the same copy rules as `longhaul run-local`
(agent/workspace.py), and that commit is what the bare repo is cloned from. A GitHub repo
arrives with LH-305, which adds the clone to `_source`.

A retried attempt must succeed after an earlier one died halfway. The earlier one can leave a
container with the network on, a volume with half a workspace, a repo and a half-built clone.
prepare_workspace therefore starts by removing everything of the task, which is safe because it
is the first step of a task and nothing else exists yet. ensure_sandbox keeps the volume, whose
venv saves the setup, and removes every container of the task before it makes the new one, so
that exactly one is live whatever the state of the old one: running, stopped, paused or gone.
`DockerSandboxManager.get` answers None for any container that does not run, and that is the
case this module replaces.

The setup command is `spec.settings.setup_command`, or the default of `longhaul run-local` when
there is none. The default is read from the pyproject.toml of the workspace, so a recovery sets
up what the workspace holds at `head_sha` and does not depend on the folder the task started
from. Its time limit is shorter than the activity's, so a setup that hangs fails with its own
output tail instead of as a timeout.

prepare_workspace also works out how the repo runs its tests (LH-603), when it is given a memory
store. After the setup and with the network cut, it reads the files that name commands, runs
each candidate once at the base commit and keeps the ones that printed a test or lint summary,
as approved items of the default branch (longhaul.memory.commands). The agent then starts with
commands that were seen to work and does not guess. The commands may write files, so the
workspace is put back on the base commit afterwards. Memory is an extra: a failure to find or to
store the commands is logged and the task goes on without them. Without a store nothing is run.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from temporalio import activity

from longhaul.agent.workspace import (
    SetupCommandError,
    SourceFile,
    WorkspaceError,
    collect_source_files,
    pip_install_command,
)
from longhaul.checkpoints.git_store import GitCheckpointStore, RepoNotFoundError, check_task_id
from longhaul.checkpoints.snapshot import snapshot_folder
from longhaul.contracts import MemoryItem, Sandbox, SandboxLimits, TaskSpec
from longhaul.db.records import Recorder
from longhaul.durable import acts
from longhaul.durable.activities.support import heartbeating, not_retried, small
from longhaul.github.intake import LOCAL_PREFIX, is_local
from longhaul.memory.commands import discover_commands

log = logging.getLogger(__name__)

# The event type of plan section 3.5 that ensure_sandbox records.
SANDBOX_RECREATED = "sandbox_recreated"
# How long the setup command may run. The shorter of the two activities that run it is
# ensure_sandbox with 10 minutes, and the container, the clone and the restore come first.
SETUP_TIMEOUT_S = 480


class WorkspaceHost(Protocol):
    """The part of DockerSandboxManager that these activities use."""

    async def create(
        self,
        task_id: str,
        image: str,
        limits: SandboxLimits,
        network: bool,
        mounts: Mapping[str, str] | None = None,
    ) -> Sandbox: ...

    async def get(self, sandbox_id: str) -> Sandbox | None: ...

    async def set_network(self, sandbox_id: str, enabled: bool) -> None: ...

    async def remove_containers(self, task_id: str) -> None: ...

    async def cleanup_task(self, task_id: str) -> None: ...


class CommandMemory(Protocol):
    """The part of PgMemoryStore that prepare_workspace uses."""

    async def add_verified(self, item: MemoryItem, *, source_ref: str) -> str: ...


@dataclass(frozen=True)
class LocalFolder:
    """A folder to commit as the base of a task, and the files of it that go in."""

    path: Path
    files: list[SourceFile]


class WorkspaceActivities:
    """prepare_workspace and ensure_sandbox, with what they need.

    `image` and `limits` are `sandbox.image` and `sandbox.limits` of the config, which the
    workflow cannot read. `checkpoints` is the store that tool_exec uses, because the repo that
    this class creates is the one that tool_exec saves into. `memory` is where the verified
    commands of a repo are kept. Without it, prepare_workspace does not look for them, which is
    how a run or a test without a database works.
    """

    def __init__(
        self,
        sandboxes: WorkspaceHost,
        checkpoints: GitCheckpointStore,
        recorder: Recorder,
        *,
        image: str,
        limits: SandboxLimits,
        setup_timeout_s: int = SETUP_TIMEOUT_S,
        memory: CommandMemory | None = None,
    ) -> None:
        self._sandboxes = sandboxes
        self._store = checkpoints
        self._recorder = recorder
        self._image = image
        self._limits = limits
        self._setup_timeout_s = setup_timeout_s
        self._memory = memory

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.prepare_workspace, self.ensure_sandbox]

    # ---------- prepare_workspace ----------
    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        spec = small(spec)
        folder = await self._source(spec)  # a bad input is refused before anything is touched
        task_id = spec.task_id
        async with heartbeating():
            try:
                await self._clear(task_id)
                base_sha = await self._publish_base(task_id, folder)
                sandbox = await self._boot(task_id)
                await self._set_up(sandbox, spec)
                await self._sandboxes.set_network(sandbox.id, False)
                await self._learn_commands(sandbox, spec, base_sha)
                head_sha = await self._store.head(task_id)
            except BaseException:  # also a cancel: a task that is cancelled must leave nothing
                await self._discard(task_id)
                raise
        return acts.PrepareWorkspaceOutput(
            sandbox_id=sandbox.id, base_sha=base_sha, head_sha=head_sha
        )

    async def _source(self, spec: TaskSpec) -> LocalFolder:
        """The folder to commit as the base, or a refusal that no retry can change.

        This is the seam for LH-305. A GitHub repo is cloned on the host here, and what it
        gives back is a place that init_task can clone from, with the sha of the base commit.
        """
        try:
            check_task_id(spec.task_id)
            if not is_local(spec.repo):
                raise NotImplementedError(
                    f"{spec.repo!r} is a GitHub repo, and cloning one arrives with LH-305. "
                    f"Only {LOCAL_PREFIX}<absolute path> repos can be prepared until then"
                )
            path = Path(spec.repo[len(LOCAL_PREFIX) :])
            if not path.is_absolute():
                raise WorkspaceError(
                    f"{spec.repo!r}: a local repo needs an absolute path, as in "
                    f"{LOCAL_PREFIX}/abs/path"
                )
            files = await asyncio.to_thread(collect_source_files, path)
        except (ValueError, NotImplementedError, WorkspaceError) as exc:
            raise not_retried(exc) from exc
        return LocalFolder(path, files)

    async def _publish_base(self, task_id: str, folder: LocalFolder) -> str:
        """Commit the folder, make the task's bare repo from the commit, and return its sha."""
        scratch = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="lh-base-"))
        try:
            repo = scratch / "repo"
            base_sha = await snapshot_folder(folder.path, folder.files, repo)
            await self._store.init_task(task_id, str(repo), base_sha)
        except WorkspaceError as exc:
            raise not_retried(exc) from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, scratch, True)
        return base_sha

    async def _learn_commands(self, sandbox: Sandbox, spec: TaskSpec, base_sha: str) -> None:
        """Keep the commands that run this repo's tests, as seen working at the base commit.

        A retried attempt runs this again and stores the same items, which the store
        de-duplicates. `source_ref` is the base sha, the commit the commands were checked at.
        """
        if self._memory is None:
            return
        try:
            for command in await discover_commands(sandbox):
                item = MemoryItem(
                    id="",  # the store makes the id
                    repo=spec.repo,
                    kind="command",
                    content=command,
                    status="approved",
                    source="default_branch",
                )
                await self._memory.add_verified(item, source_ref=base_sha)
        except Exception as exc:  # a cancel is not an Exception and passes through
            log.warning(
                "could not keep the commands of %s for task %s: %s", spec.repo, spec.task_id, exc
            )
        # Whatever the commands wrote must not reach the agent: a formatter in a lint target
        # edits tracked files, and a build leaves new ones. The venv of the setup is kept.
        if not await self._store.is_at(sandbox, base_sha):
            await self._store.restore(sandbox, base_sha)

    # ---------- ensure_sandbox ----------
    @activity.defn(name=acts.ENSURE_SANDBOX.name)
    async def ensure_sandbox(self, inp: acts.EnsureSandboxInput) -> str:
        inp = small(inp)
        running = await self._sandboxes.get(inp.sandbox_id) if inp.sandbox_id else None
        if running is not None:
            return running.id
        self._require_repo(inp.task_id)
        async with heartbeating():
            sandbox = await self._rebuild(inp)
        await self._recorder.record_event(
            inp.task_id,
            SANDBOX_RECREATED,
            {
                "head_sha": inp.head_sha,
                "previous_sandbox_id": inp.sandbox_id,
                "sandbox_id": sandbox.id,
            },
        )
        log.info(
            "sandbox of task %s recreated at %s: %s replaces %s",
            inp.task_id,
            inp.head_sha,
            sandbox.id,
            inp.sandbox_id,
        )
        return sandbox.id

    def _require_repo(self, task_id: str) -> None:
        """Without the bare repo there is nothing to return to, and asking again changes nothing.

        That is failure mode F4: the host that held the repo is gone. Building the repo again
        from the latest bundle (GitCheckpointStore.restore_from_bundle) is not wired in yet.
        """
        try:
            found = self._store.has_repo(task_id)
        except ValueError as exc:
            raise not_retried(exc) from exc
        if not found:
            raise not_retried(
                RepoNotFoundError(
                    f"task {task_id} has no checkpoint repo on this host (failure mode F4), "
                    "so its sandbox cannot be recreated"
                )
            )

    async def _rebuild(self, inp: acts.EnsureSandboxInput) -> Sandbox:
        task_id = inp.task_id
        # What is left of the old container goes first, and so does what an earlier attempt of
        # this activity made and did not get to report. Exactly one container may be live.
        await self._sandboxes.remove_containers(task_id)
        try:
            sandbox = await self._boot(task_id)
            await self._store.restore(sandbox, inp.head_sha)
            await self._set_up(sandbox, inp.spec)
            await self._sandboxes.set_network(sandbox.id, False)
        except BaseException:
            await self._drop_containers(task_id)  # the volume stays for the next attempt
            raise
        return sandbox

    # ---------- what both need ----------
    async def _boot(self, task_id: str) -> Sandbox:
        """A container with the network on and the repo at /ckpt, and a workspace cloned from it."""
        sandbox = await self._sandboxes.create(
            task_id,
            self._image,
            self._limits,
            network=True,
            mounts=self._store.mounts(task_id),
        )
        await self._store.attach(sandbox, task_id)
        return sandbox

    async def _set_up(self, sandbox: Sandbox, spec: TaskSpec) -> None:
        command = await self._setup_command(sandbox, spec)
        if not command:
            return
        result = await sandbox.exec(command, timeout_s=self._setup_timeout_s)
        if result.exit_code != 0 or result.timed_out:
            raise SetupCommandError(command, result, self._setup_timeout_s)

    @staticmethod
    async def _setup_command(sandbox: Sandbox, spec: TaskSpec) -> str | None:
        configured = spec.settings.setup_command
        if configured is not None:
            return configured.strip()  # an empty string means no setup
        try:
            text = (await sandbox.read_file("pyproject.toml")).decode("utf-8")
        except (FileNotFoundError, IsADirectoryError):
            return None  # nothing to install
        except UnicodeDecodeError:
            text = ""  # a file that is not text names no dev extra
        return pip_install_command(text)

    async def _clear(self, task_id: str) -> None:
        """Remove everything of the task: containers, volume, repo and half-built clones."""
        await self._sandboxes.cleanup_task(task_id)
        await self._store.cleanup_task(task_id)

    async def _discard(self, task_id: str) -> None:
        # A failure here is logged, so it cannot replace the error that led to the clean-up.
        try:
            await self._clear(task_id)
        except Exception as exc:
            log.warning("could not remove what the preparation of task %s made: %s", task_id, exc)

    async def _drop_containers(self, task_id: str) -> None:
        try:
            await self._sandboxes.remove_containers(task_id)
        except Exception as exc:
            log.warning("could not remove the new container of task %s: %s", task_id, exc)
