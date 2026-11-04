"""Clone from GitHub and push from the host, with a real sandbox (LH-305).

GitHub is a FakeGitHub whose bare remote is a local repo, so the clone and the push are real git
commands against a path and nothing leaves the machine. prepare_workspace runs for real on Docker:
the repo is cloned into the task's bare repo on the host, the container gets that repo at /ckpt,
and the checks look at what the container can see. The token must be nowhere in it: not in its
environment, its git config, /ckpt/config or the settings Docker holds for it.

Run `make sandbox-image` first. The setup command is empty, so nothing is installed and the
tests need no outbound network. Every task id and container has a random suffix, and each test
removes what it made.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import docker
import pytest
from temporalio.testing import ActivityEnvironment
from tests.fakes.calc_remote import REPO, TOKEN, Github, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub, git

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.checkpoints.push import ForbiddenBranchError, push_branch
from longhaul.contracts import RepoSettings, Sandbox, SandboxLimits, TaskSpec
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.sandbox.docker_manager import DockerSandboxManager

pytestmark = [pytest.mark.integration, pytest.mark.phase3]

IMAGE = "longhaul-sandbox:py312"
MODULE = "src/calc/__init__.py"
FIXED = "def add(a: int, b: int) -> int:\n    return a + b\n"
HEADER = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
# What must not appear anywhere the sandbox can look.
SECRETS = (TOKEN, HEADER)


@dataclass
class World:
    task_id: str
    fake: FakeGitHub
    github: Github
    store: GitCheckpointStore
    manager: DockerSandboxManager
    activities: WorkspaceActivities
    base_sha: str

    @property
    def branch(self) -> str:
        return "longhaul/42"

    def spec(self) -> TaskSpec:
        return TaskSpec(
            task_id=self.task_id,
            repo=REPO,
            issue_number=42,
            title="add() subtracts",
            body="`calc.add` subtracts its arguments instead of adding them.",
            settings=RepoSettings(repo=REPO, setup_command=""),
        )

    async def prepare(self) -> tuple[acts.PrepareWorkspaceOutput, Sandbox]:
        out: acts.PrepareWorkspaceOutput = await ActivityEnvironment().run(
            self.activities.prepare_workspace, self.spec()
        )
        sandbox = await self.manager.get(out.sandbox_id)
        assert sandbox is not None
        return out, sandbox

    async def push(self, local: str, remote_branch: str) -> str:
        return await push_branch(
            self.task_id,
            local,
            remote_branch,
            repo=REPO,
            store=self.store,
            remote=self.github.remote,
        )


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    # The id ends in the issue number, so the scratch branch is longhaul/42.
    task_id = f"lh-clone{uuid.uuid4().hex[:8]}-42"
    fake = FakeGitHub()
    base_sha = seed_calc(fake, tmp_path / "gh")
    github = github_for(fake)
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
    manager = DockerSandboxManager()
    activities = WorkspaceActivities(
        manager,
        store,
        MemoryRecorder(),
        image=IMAGE,
        limits=SandboxLimits(),
        github=github.remote,
    )
    try:
        yield World(task_id, fake, github, store, manager, activities, base_sha)
    finally:
        await manager.cleanup_task(task_id)
        await store.cleanup_task(task_id)
        await github.aclose()


async def sh(box: Sandbox, script: str) -> str:
    """Run a probe that must succeed and return its stdout."""
    result = await box.exec(script, timeout_s=120)
    assert result.exit_code == 0, f"{script}\n{result.stdout}\n{result.stderr}"
    return result.stdout.strip()


async def test_prepare_from_remote(world: World) -> None:
    out, box = await world.prepare()

    # The sandbox is at the head of the default branch, which the API reported.
    assert out.base_sha == out.head_sha == world.base_sha
    assert await sh(box, "git rev-parse HEAD") == world.base_sha
    assert await sh(box, "git rev-parse refs/tags/base") == world.base_sha
    assert await sh(box, "git branch --show-current") == world.branch
    assert await sh(box, "git status --porcelain") == ""
    # It holds what the remote's commit holds, file for file.
    remote = Path(world.fake.clone_url(REPO))
    held = git("ls-tree", "-r", "--name-only", world.base_sha, cwd=remote).splitlines()
    assert (await sh(box, "git ls-files")).splitlines() == held
    assert "src/calc/__init__.py" in held
    # The task branch of the bare repo on the host starts at the same commit.
    assert await world.store.head(world.task_id) == world.base_sha
    # Continued as for a local repo: the network is cut once the preparation is done.
    info = docker.from_env().api.inspect_container(box.id)
    assert set(info["NetworkSettings"]["Networks"]) == set()


async def test_no_token_in_sandbox_or_ckpt_config(world: World) -> None:
    out, box = await world.prepare()
    await box.write_file(MODULE, FIXED.encode())
    checkpoint = await world.store.commit(box, "lh: edit_file turn 1 call c1")
    assert checkpoint is not None
    # The push runs on the host and signs in there. It must leave nothing in the sandbox's reach.
    await world.push(world.branch, "longhaul-pr/42")

    environment = await sh(box, "env")
    assert not any(secret in environment for secret in SECRETS)
    ckpt_config = await sh(box, "cat /ckpt/config")
    assert not any(secret in ckpt_config for secret in SECRETS)
    assert "[remote" not in ckpt_config, "the clone's origin was removed"
    git_config = await sh(box, "git config --list --show-origin")
    assert not any(secret in git_config for secret in SECRETS)
    for secret in SECRETS:
        found = await box.exec(f"grep -rlF -e '{secret}' /ckpt /workspace", timeout_s=120)
        assert found.exit_code == 1, f"{secret!r} is in: {found.stdout}"
    # What Docker holds for the container: its environment, command and mounts.
    settings = json.dumps(docker.from_env().api.inspect_container(box.id))
    assert not any(secret in settings for secret in SECRETS)
    # And none of it is in the host-side record of the repo either.
    config = world.store.repo_path(world.task_id).joinpath("config").read_text()
    assert not any(secret in config for secret in SECRETS)
    assert out.head_sha == world.base_sha


async def test_a_checkpoint_is_pushed_from_the_host_and_the_default_branch_is_not(
    world: World,
) -> None:
    _, box = await world.prepare()
    await box.write_file(MODULE, FIXED.encode())
    checkpoint = await world.store.commit(box, "lh: edit_file turn 1 call c1")
    assert checkpoint is not None
    before = world.fake.refs(REPO)

    with pytest.raises(ForbiddenBranchError):
        await world.push(world.branch, "main")
    assert world.fake.refs(REPO) == before, "a refused push changes nothing"

    pushed = await world.push(world.branch, "longhaul-pr/42")

    assert pushed == checkpoint
    assert world.fake.branches(REPO)["longhaul-pr/42"] == checkpoint
    assert world.fake.branches(REPO)["main"] == world.base_sha
