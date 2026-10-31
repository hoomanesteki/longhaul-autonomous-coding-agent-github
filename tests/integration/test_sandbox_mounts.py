"""Host folders mounted into a sandbox (LH-205): the checkpoint repo is the first user."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import docker
import pytest

from longhaul.contracts import SandboxLimits
from longhaul.sandbox.docker_manager import TASK_LABEL, DockerSandboxManager

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

IMAGE = "longhaul-sandbox:py312"


@pytest.fixture
def manager() -> DockerSandboxManager:
    return DockerSandboxManager()


@pytest.fixture
async def task_id(manager: DockerSandboxManager) -> AsyncIterator[str]:
    tid = f"t{uuid.uuid4().hex[:10]}"
    try:
        yield tid
    finally:
        await manager.cleanup_task(tid)


async def test_a_mounted_folder_is_shared_with_the_host_and_writable_by_the_agent(
    manager: DockerSandboxManager, task_id: str, tmp_path: Path
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "from-host.txt").write_text("hello\n")
    # On a Linux host the agent user (uid 10001) is "other" to the folder, so it needs the
    # bits. On Docker Desktop the mount maps ownership and the bits make no difference.
    shared.chmod(0o777)

    sandbox = await manager.create(
        task_id, IMAGE, SandboxLimits(), network=False, mounts={str(shared): "/data"}
    )
    result = await sandbox.exec(
        "id -u && cat /data/from-host.txt && echo from-agent > /data/from-agent.txt && echo ok",
        timeout_s=30,
    )

    assert result.stdout.split() == ["10001", "hello", "ok"], result.stderr
    assert (shared / "from-agent.txt").read_text() == "from-agent\n"


async def test_a_mount_leaves_the_other_guards_in_place(
    manager: DockerSandboxManager, task_id: str, tmp_path: Path
) -> None:
    sandbox = await manager.create(
        task_id, IMAGE, SandboxLimits(), network=False, mounts={str(tmp_path): "/data"}
    )

    mounts = (await sandbox.exec("cat /proc/mounts", timeout_s=30)).stdout.splitlines()
    root = next(line.split() for line in mounts if line.split()[1] == "/")
    data = next(line.split() for line in mounts if line.split()[1] == "/data")
    assert "ro" in root[3].split(",")
    assert "rw" in data[3].split(",")
    assert (await sandbox.exec("touch /usr/probe", timeout_s=30)).exit_code != 0
    routes = (await sandbox.exec("awk 'NR>1 {print $1}' /proc/net/route", timeout_s=30)).stdout
    assert routes.split() == []
    # /data is the only host folder in the container.
    attrs = docker.from_env().api.inspect_container(sandbox.id)
    binds = [m for m in attrs["Mounts"] if m["Type"] == "bind"]
    assert [(m["Destination"], m["RW"]) for m in binds] == [("/data", True)]


async def test_a_bad_mount_is_refused_before_any_container_exists(
    manager: DockerSandboxManager, task_id: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        await manager.create(
            task_id,
            IMAGE,
            SandboxLimits(),
            network=False,
            mounts={str(tmp_path / "missing"): "/data"},
        )

    found = docker.from_env().containers.list(
        all=True, filters={"label": f"{TASK_LABEL}={task_id}"}
    )
    assert found == []
    assert not (tmp_path / "missing").exists()
