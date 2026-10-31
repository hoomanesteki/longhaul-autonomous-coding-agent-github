"""Local workspace preparation on a real Docker sandbox, with the calc fixture.

Run `make sandbox-image` first. The default setup builds a venv and installs pytest and
calc from PyPI, so these tests need outbound network, as the other sandbox tests do. One
workspace serves the tests that read it, because the install is slow; the tests that
break something make their own.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import docker
import pytest
import pytest_asyncio
from docker.errors import NotFound

from longhaul.agent.workspace import LocalWorkspace, SetupCommandError, prepare_local_workspace
from longhaul.contracts import Sandbox, SandboxLimits, ToolCall, ToolContext
from longhaul.sandbox.docker_manager import TASK_LABEL, DockerSandboxManager, volume_name
from longhaul.tools.git import NO_CHANGES
from longhaul.tools.registry import default_registry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.phase1,
    pytest.mark.asyncio(loop_scope="module"),
]

IMAGE = "longhaul-sandbox:py312"
CALC = Path(__file__).parent.parent / "fixtures" / "repos" / "calc"
CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")


def new_task_id() -> str:
    return f"t{uuid.uuid4().hex[:10]}"


async def sh(box: Sandbox, script: str) -> str:
    """Run a probe that must succeed and return its stdout."""
    r = await box.exec(script, timeout_s=120)
    assert r.exit_code == 0, f"{script}\n{r.stdout}\n{r.stderr}"
    return r.stdout.strip()


def networks(container_id: str) -> set[str]:
    """The networks Docker says the container is attached to."""
    info = docker.from_env().api.inspect_container(container_id)
    return set(info["NetworkSettings"]["Networks"])


def container_exists(container_id: str) -> bool:
    try:
        docker.from_env().api.inspect_container(container_id)
    except NotFound:
        return False
    return True


def task_containers(task_id: str) -> list[str]:
    found = docker.from_env().containers.list(
        all=True, filters={"label": f"{TASK_LABEL}={task_id}"}
    )
    return [str(c.id) for c in found]


def volume_exists(task_id: str) -> bool:
    try:
        docker.from_env().volumes.get(volume_name(task_id))
    except NotFound:
        return False
    return True


@pytest.fixture
def manager() -> DockerSandboxManager:
    return DockerSandboxManager()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def workspace() -> AsyncIterator[LocalWorkspace]:
    """calc, prepared with the default setup: a venv with pytest and calc installed."""
    shared = DockerSandboxManager()
    task_id = new_task_id()
    try:
        yield await prepare_local_workspace(
            shared, CALC, task_id=task_id, image=IMAGE, limits=SandboxLimits(), setup_command=None
        )
    finally:
        await shared.cleanup_task(task_id)


async def test_the_base_tag_exists_and_the_tree_is_clean(workspace: LocalWorkspace) -> None:
    box, base_sha = workspace
    assert len(base_sha) == 40
    assert await sh(box, "git rev-parse refs/tags/base") == base_sha
    assert await sh(box, "git rev-parse HEAD") == base_sha
    assert await sh(box, "git rev-list --count HEAD") == "1"
    # Clean after the setup, so the venv and the egg-info the install left are excluded.
    assert await sh(box, "git status --porcelain") == ""
    assert await sh(box, "test -x .venv/bin/python && ls -d src/*.egg-info") == "src/calc.egg-info"


async def test_the_reference_fix_and_the_issue_text_are_not_in_the_workspace(
    workspace: LocalWorkspace,
) -> None:
    box, _ = workspace
    for name in ("REFERENCE.patch", "ISSUE.md", ".longhaulignore"):
        assert (await box.exec(f"test -e {name}", timeout_s=10)).exit_code != 0, name
    tracked = (await sh(box, "git ls-files")).splitlines()
    assert tracked == [".gitignore", "pyproject.toml", "src/calc/__init__.py", "tests/test_calc.py"]
    # The fix itself is nowhere in the history either, not only the file that carries it.
    history = await sh(box, "git log --all -p")
    assert "return a - b" in history and "return a + b" not in history


async def test_pytest_runs_inside_and_one_test_fails_at_base(workspace: LocalWorkspace) -> None:
    box, _ = workspace
    assert await sh(box, "command -v python") == "/workspace/.venv/bin/python"
    r = await box.exec("python -m pytest -q", timeout_s=120)
    assert r.exit_code == 1, r.stdout + r.stderr
    assert "1 failed, 2 passed" in r.stdout
    assert "test_add_positive_numbers" in r.stdout
    # Running the tests leaves nothing that would show up in the agent's diff.
    assert await sh(box, "git status --porcelain") == ""


async def test_the_git_diff_tool_sees_no_change_at_base(workspace: LocalWorkspace) -> None:
    box, _ = workspace
    call = ToolCall(id="c", name="git_diff", input={})
    result = await default_registry().run(call, box, CTX)
    assert result.ok and result.output == NO_CHANGES


async def test_the_network_is_detached_afterwards(workspace: LocalWorkspace) -> None:
    box, _ = workspace
    assert networks(box.id) == set()
    routes = await sh(box, "awk 'NR>1 {print $1}' /proc/net/route")
    assert routes == ""


async def test_setup_runs_with_the_network_on(manager: DockerSandboxManager) -> None:
    # Structural, so it holds without internet: the setup command sees a route out,
    # and Docker lists no network once the preparation has returned.
    task_id = new_task_id()
    probe = "awk 'NR>1 {print $1}' /proc/net/route | grep -qx eth0 && echo online > saw-network"
    try:
        box, _ = await prepare_local_workspace(
            manager,
            CALC,
            task_id=task_id,
            image=IMAGE,
            limits=SandboxLimits(),
            setup_command=probe,
        )
        assert await sh(box, "cat saw-network") == "online"
        assert networks(box.id) == set()
        assert await sh(box, "awk 'NR>1 {print $1}' /proc/net/route") == ""
        # The file the setup wrote is the setup's, not part of the base commit.
        assert "saw-network" not in await sh(box, "git ls-tree -r --name-only refs/tags/base")
    finally:
        await manager.cleanup_task(task_id)


async def test_a_repo_without_a_gitignore_still_has_a_clean_tree_after_setup(
    manager: DockerSandboxManager, tmp_path: Path
) -> None:
    # calc's own .gitignore already lists the venv and the egg-info. Without it, only the
    # exclude file written by the preparation keeps them out of `git status` and the diff.
    bare = tmp_path / "calc"
    shutil.copytree(CALC, bare, ignore=shutil.ignore_patterns(".gitignore"))
    task_id = new_task_id()
    try:
        box, _ = await prepare_local_workspace(
            manager, bare, task_id=task_id, image=IMAGE, limits=SandboxLimits()
        )
        assert await sh(box, "test -d .venv && ls -d src/*.egg-info") == "src/calc.egg-info"
        assert await sh(box, "git status --porcelain") == ""
        tracked = (await sh(box, "git ls-files")).splitlines()
        assert tracked == ["pyproject.toml", "src/calc/__init__.py", "tests/test_calc.py"]
    finally:
        await manager.cleanup_task(task_id)


async def test_the_executable_bit_is_kept_in_the_files_and_the_base_commit(
    manager: DockerSandboxManager, tmp_path: Path
) -> None:
    source = tmp_path / "scripts-repo"
    (source / "bin").mkdir(parents=True)
    (source / "bin" / "hello.sh").write_text("#!/bin/sh\necho hello\n")
    (source / "bin" / "hello.sh").chmod(0o755)
    (source / "notes.txt").write_text("plain\n")
    task_id = new_task_id()
    try:
        box, _ = await prepare_local_workspace(
            manager, source, task_id=task_id, image=IMAGE, limits=SandboxLimits(), setup_command=""
        )
        assert await sh(box, "bin/hello.sh") == "hello"
        modes = await sh(box, "git ls-files -s | awk '{print $1, $4}'")
        assert modes.splitlines() == ["100755 bin/hello.sh", "100644 notes.txt"]
        assert await sh(box, "git status --porcelain") == ""
    finally:
        await manager.cleanup_task(task_id)


async def test_a_failing_setup_raises_with_the_output_tail_and_cleans_up(
    manager: DockerSandboxManager,
) -> None:
    task_id = new_task_id()
    try:
        with pytest.raises(SetupCommandError) as caught:
            await prepare_local_workspace(
                manager,
                CALC,
                task_id=task_id,
                image=IMAGE,
                limits=SandboxLimits(),
                setup_command="echo resolving; echo 'ERROR: no matching distribution' >&2; exit 3",
            )
        message = str(caught.value)
        assert "exit code 3" in message
        assert "ERROR: no matching distribution" in message and "resolving" in message
        assert task_containers(task_id) == []
        assert not volume_exists(task_id)
    finally:
        await manager.cleanup_task(task_id)


async def test_the_sandbox_and_its_volume_go_away_with_cleanup_task(
    manager: DockerSandboxManager,
) -> None:
    task_id = new_task_id()
    try:
        box, _ = await prepare_local_workspace(
            manager,
            CALC,
            task_id=task_id,
            image=IMAGE,
            limits=SandboxLimits(),
            setup_command="true",
        )
        container_id = box.id
        assert container_exists(container_id) and volume_exists(task_id)
    finally:
        await manager.cleanup_task(task_id)
    assert not container_exists(container_id)
    assert task_containers(task_id) == []
    assert not volume_exists(task_id)
