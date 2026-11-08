"""The sandbox's container flags, read back from Docker (LH-906).

The sandbox manager sets its guards when it starts a container, and the other sandbox tests
check what a command inside can or cannot do. This file checks the other side: what `docker
inspect` says about a live container. A flag that the manager stopped passing would leave the
behaviour tests green on a Docker host that happens to enforce the same thing some other way,
and this is the test that notices. docs/security.md maps each flag to the test below.

The container is created with a limit that differs from every default, so a flag that was
copied from a constant and not from the SandboxLimits of the call shows up as well.

Every task id has a random suffix, and the fixture removes the container and the volume that
the test made.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import docker
import pytest

from longhaul.contracts import Sandbox, SandboxLimits
from longhaul.sandbox.docker_manager import AGENT_UID, AGENT_USER, DockerSandboxManager

pytestmark = [pytest.mark.integration, pytest.mark.phase9]

IMAGE = "longhaul-sandbox:py312"
LIMITS = SandboxLimits(cpus=1.5, memory_mb=1024, pids=200)


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


def _inspect(sandbox: Sandbox) -> dict[str, Any]:
    return docker.from_env().api.inspect_container(sandbox.id)


async def test_inspect_flags(manager: DockerSandboxManager, task_id: str) -> None:
    # Network on first, as during setup, so that the "after setup" state below comes from
    # the same calls the workspace code makes and not from a container that never had one.
    sandbox = await manager.create(task_id, IMAGE, LIMITS, network=True)
    before = _inspect(sandbox)
    assert list(before["NetworkSettings"]["Networks"]) == ["bridge"]
    await manager.set_network(sandbox.id, False)
    attrs = _inspect(sandbox)
    host = attrs["HostConfig"]

    # Capabilities: none kept, none added back, and not a privileged container.
    assert host["CapDrop"] == ["ALL"]
    assert not host["CapAdd"]
    assert host["Privileged"] is False
    assert host["ReadonlyRootfs"] is True
    assert "no-new-privileges:true" in host["SecurityOpt"]

    # The image runs as the user named `agent`, which is uid 10001 in docker/sandbox/Dockerfile.
    # The manager also passes the name, so a changed image cannot make the container root.
    assert attrs["Config"]["User"] == AGENT_USER == "agent"
    assert AGENT_UID == 10001

    # Limits come from the SandboxLimits of the call, not from a default.
    assert host["PidsLimit"] == LIMITS.pids
    assert host["Memory"] == LIMITS.memory_mb * 1024 * 1024
    assert host["NanoCpus"] == int(LIMITS.cpus * 1e9)

    # Network: the container starts on the bridge because Docker cannot move a `none`
    # container onto a network later, so HostConfig.NetworkMode keeps saying "bridge". What
    # counts is that nothing is attached once setup is over.
    assert attrs["NetworkSettings"]["Networks"] == {}

    # Nothing of the host is shared with the container beyond the task's own volume and
    # the two tmpfs folders: no host network, pid or ipc namespace, no Docker socket.
    assert host["PidMode"] == ""
    assert host["IpcMode"] != "host"
    assert host["UsernsMode"] == ""
    mounts = {m["Destination"]: m["Type"] for m in attrs["Mounts"]}
    assert mounts == {"/workspace": "volume"}
    assert set(host["Tmpfs"]) == {"/tmp", "/home/agent"}


async def test_the_flags_hold_inside_the_container(
    manager: DockerSandboxManager, task_id: str
) -> None:
    """What the flags mean from the agent's side, read in the container's own terms."""
    sandbox = await manager.create(task_id, IMAGE, LIMITS, network=False)

    result = await sandbox.exec(
        "id -u; grep -E '^Cap(Eff|Prm|Bnd)' /proc/self/status; grep NoNewPrivs /proc/self/status",
        timeout_s=30,
    )

    lines = result.stdout.splitlines()
    assert lines[0] == "10001", result.stderr
    caps = [line.split()[1] for line in lines[1:4]]
    assert caps == ["0000000000000000"] * 3
    assert lines[4].split()[1] == "1"
    # A sandbox made with network=False never has a route, and Docker lists no network for it.
    assert _inspect(sandbox)["NetworkSettings"]["Networks"] == {}
    routes = await sandbox.exec("awk 'NR>1 {print $1}' /proc/net/route", timeout_s=30)
    assert routes.stdout.split() == []
