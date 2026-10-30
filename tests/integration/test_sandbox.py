import json
import os
import subprocess
import uuid
from collections.abc import AsyncIterator

import docker
import pytest
from docker.errors import NotFound

from longhaul.contracts import Sandbox, SandboxLimits
from longhaul.sandbox.docker_manager import ENV_ALLOWLIST, DockerSandboxManager, volume_name

pytestmark = [pytest.mark.integration, pytest.mark.phase1]

IMAGE = "longhaul-sandbox:py312"
PYPI = "python -c 'import urllib.request as u; u.urlopen(\"https://pypi.org\", timeout=5)'"


def _online() -> bool:
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            "import urllib.request as u; u.urlopen('https://pypi.org', timeout=5)",
        ],
        capture_output=True,
        check=False,
    )
    return r.returncode == 0


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


async def _make(manager: DockerSandboxManager, tid: str, network: bool = False) -> Sandbox:
    return await manager.create(tid, IMAGE, SandboxLimits(), network=network)


async def test_runs_as_non_root(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    r = await sbx.exec("id -u", timeout_s=10)
    assert r.stdout.strip() == "10001"
    # The workspace volume must be writable by the agent user.
    w = await sbx.exec("touch /workspace/x && git init -q /workspace/r && echo ok", timeout_s=20)
    assert w.stdout.strip() == "ok", w.stderr
    # PATH survives (no login-shell reset) and HOME is writable.
    h = await sbx.exec("touch $HOME/f && command -v git", timeout_s=10)
    assert h.exit_code == 0, h.stderr


async def test_no_network_when_disabled(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id, network=False)
    r = await sbx.exec(PYPI, timeout_s=20)
    assert r.exit_code != 0


async def test_network_toggle(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id, network=True)
    online = _online()
    if online:
        assert (await sbx.exec(PYPI, timeout_s=20)).exit_code == 0
    else:
        print("no outbound network on this machine; checking the disabled case only")
    await manager.set_network(sbx.id, False)
    assert (await sbx.exec(PYPI, timeout_s=20)).exit_code != 0
    await manager.set_network(sbx.id, True)
    await manager.set_network(sbx.id, False)  # idempotent in both directions
    assert (await sbx.exec(PYPI, timeout_s=20)).exit_code != 0


async def test_timeout_kills(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    r = await sbx.exec("sleep 30", timeout_s=2)
    assert r.timed_out
    assert r.duration_ms < 5000


async def test_no_secrets_in_container_env(
    manager: DockerSandboxManager, task_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LONGHAUL_GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    sbx = await _make(manager, task_id)
    client = docker.from_env()
    # Variables baked into the image (python version, GPG key id) are not secrets
    # and Docker always merges them in, so they are allowed too.
    image_env = {e.split("=", 1)[0] for e in client.images.get(IMAGE).attrs["Config"]["Env"]}
    allowed = set(ENV_ALLOWLIST) | image_env | {"HOSTNAME"}

    inside = (await sbx.exec("env", timeout_s=10)).stdout
    names = {line.split("=", 1)[0] for line in inside.splitlines() if "=" in line}
    # bash adds its own bookkeeping variables.
    names -= {"PWD", "SHLVL", "_", "OLDPWD", "HOSTNAME"}
    assert names <= allowed, names - allowed
    assert "secret" not in inside

    cfg_env = client.api.inspect_container(sbx.id)["Config"]["Env"]
    assert {e.split("=", 1)[0] for e in cfg_env} <= allowed
    assert "secret" not in json.dumps(cfg_env)
    assert "ANTHROPIC_API_KEY" in os.environ


async def test_file_roundtrip_binary(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    data = bytes(range(256))
    await sbx.write_file("deep/er/blob.bin", data)
    assert await sbx.read_file("deep/er/blob.bin") == data
    assert await sbx.read_file("/workspace/deep/er/blob.bin") == data
    r = await sbx.exec(
        "stat -c %u:%g deep/er/blob.bin deep/er; echo x >> deep/er/new", timeout_s=10
    )
    assert r.stdout.split() == ["10001:10001", "10001:10001"], r.stderr
    assert r.exit_code == 0


async def test_cleanup(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    client = docker.from_env()
    assert await manager.get(sbx.id) is not None
    await manager.cleanup_task(task_id)
    with pytest.raises(NotFound):
        client.containers.get(sbx.id)
    with pytest.raises(NotFound):
        client.volumes.get(volume_name(task_id))
    assert await manager.get(sbx.id) is None


async def test_destroy_keeps_volume(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    await sbx.destroy()
    assert await manager.get(sbx.id) is None
    docker.from_env().volumes.get(volume_name(task_id))


async def test_hardening_flags(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    info = docker.from_env().api.inspect_container(sbx.id)
    host = info["HostConfig"]
    assert host["CapDrop"] == ["ALL"]
    assert host["ReadonlyRootfs"] is True
    assert info["Config"]["User"] == "agent"
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert host["PidsLimit"] == SandboxLimits().pids
    assert host["Memory"] == SandboxLimits().memory_mb * 1024 * 1024
    assert host["NanoCpus"] == 2_000_000_000
    assert info["Config"]["Labels"]["longhaul.task_id"] == task_id
    assert info["Name"].startswith(f"/lh-sbx-{task_id}-")
