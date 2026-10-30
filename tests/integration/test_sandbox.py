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


async def _sh(sbx: Sandbox, script: str) -> str:
    """Run a setup or probe script that must succeed and return its stdout."""
    r = await sbx.exec(script, timeout_s=60)
    assert r.exit_code == 0, r.stderr
    return r.stdout


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


async def test_read_file_is_confined_to_the_writable_roots(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "ln -s /etc/passwd pwlink && ln -s /etc etclink && echo ok > good.txt")
    # ValueError is what the fake raises for the same paths, so both are checked.
    escapes = [
        "../../etc/passwd",
        "/etc/passwd",
        "//etc/passwd",
        "/workspace/../etc/passwd",
        "/proc/self/environ",
        "pwlink",  # a symlink to a file outside
        "etclink/passwd",  # a symlink to a directory outside
    ]
    for path in escapes:
        with pytest.raises(PermissionError):
            await sbx.read_file(path)
        with pytest.raises(ValueError):
            await sbx.read_file(path)
    assert await sbx.read_file("good.txt") == b"ok\n"


async def test_read_file_follows_links_that_stay_inside_the_workspace(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "mkdir src && echo mod > src/mod.py && ln -s src/mod.py modlink")
    await _sh(sbx, "ln -s src srclink && ln -s ../src/mod.py src/up")
    # As open() would: a file link, a directory link and a relative link.
    assert await sbx.read_file("modlink") == b"mod\n"
    assert await sbx.read_file("srclink/mod.py") == b"mod\n"
    assert await sbx.read_file("src/up") == b"mod\n"
    assert await sbx.read_file("/workspace/srclink/up") == b"mod\n"


async def test_tmpfs_mounts_are_refused_instead_of_looking_empty(
    manager: DockerSandboxManager, task_id: str
) -> None:
    # Docker's archive API cannot see /tmp or the home directory. Without a clear error
    # a file a command just wrote there would read as "not found".
    sbx = await _make(manager, task_id)
    await _sh(sbx, "printf tmp > /tmp/t.txt && printf home > $HOME/h.txt && ln -s /tmp tmplink")
    for path in ("/tmp/t.txt", "/home/agent/h.txt", "tmplink/t.txt"):
        with pytest.raises(PermissionError, match="tmpfs"):
            await sbx.read_file(path)
        with pytest.raises(PermissionError, match="tmpfs"):
            await sbx.write_file(path, b"x")


async def test_read_file_errors_match_the_fake(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "mkdir -p src/pkg && echo keep > README.md && ln -s nowhere dangling")
    with pytest.raises(FileNotFoundError):
        await sbx.read_file("missing.txt")
    with pytest.raises(FileNotFoundError):
        await sbx.read_file("dangling")
    with pytest.raises(IsADirectoryError):
        await sbx.read_file("src")
    with pytest.raises(IsADirectoryError):
        await sbx.read_file(".")
    with pytest.raises(NotADirectoryError):
        await sbx.read_file("README.md/x")


async def test_a_missing_container_still_raises_not_found(
    manager: DockerSandboxManager, task_id: str
) -> None:
    # Recovery (F3) tells a lost container from a lost path by this exception.
    sbx = await _make(manager, task_id)
    await sbx.destroy()
    with pytest.raises(NotFound):
        await sbx.read_file("anything.txt")
    with pytest.raises(NotFound):
        await sbx.write_file("anything.txt", b"x")
    with pytest.raises(NotFound):
        await sbx.exec("true", timeout_s=5)


async def test_write_file_stamps_a_current_mtime(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await sbx.write_file("fresh.txt", b"x")
    # An mtime of 0 (1970) made Python trust stale bytecode and broke make-style checks.
    # Both clocks are read inside the container, so the test cannot depend on the host's.
    age = int(await _sh(sbx, "echo $(( $(date +%s) - $(stat -c %Y fresh.txt) ))"))
    assert abs(age) < 300


async def test_write_file_keeps_the_mode_of_an_existing_file(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "printf '#!/bin/sh\\necho one\\n' > run.sh && chmod 755 run.sh")
    await sbx.write_file("run.sh", b"#!/bin/sh\necho two\n")
    # An edit must not strip the executable bit: git would record the mode change.
    assert (await _sh(sbx, "stat -c %a run.sh && ./run.sh")).split() == ["755", "two"]
    await sbx.write_file("fresh.txt", b"x")
    assert (await _sh(sbx, "stat -c %a fresh.txt")).strip() == "644"


async def test_write_file_leaves_existing_directories_alone(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "mkdir -p keep/deep && chmod 700 keep keep/deep")
    await sbx.write_file("keep/deep/new.txt", b"x")
    await sbx.write_file("keep/made/later.txt", b"y")
    assert (await _sh(sbx, "stat -c %a keep keep/deep")).split() == ["700", "700"]
    # New directories belong to the agent user, not to root.
    assert (await _sh(sbx, "stat -c %U:%a keep/made")).strip() == "agent:755"


async def test_write_file_refuses_to_replace_a_directory(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "mkdir -p src/pkg && echo a > src/pkg/a.py")
    with pytest.raises(IsADirectoryError):
        await sbx.write_file("src", b"oops")
    with pytest.raises(IsADirectoryError):
        await sbx.write_file("/workspace/src/pkg", b"oops")
    with pytest.raises(IsADirectoryError):
        await sbx.write_file(".", b"oops")
    # The tree is exactly as it was: nothing was deleted or turned into a file.
    assert (await _sh(sbx, "test -d src/pkg && cat src/pkg/a.py")).strip() == "a"


async def test_write_file_refuses_a_parent_that_is_a_file(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "echo keep > README.md")
    for path in ("README.md/x", "README.md/deeper/x"):
        with pytest.raises(NotADirectoryError):
            await sbx.write_file(path, b"z")
    assert (await _sh(sbx, "test -f README.md && cat README.md")).strip() == "keep"


async def test_write_file_follows_symlinks_that_stay_inside_the_workspace(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "mkdir src && echo old > src/mod.py && ln -s src srclink && ln -s src/mod.py m")
    await sbx.write_file("srclink/new.py", b"through the link")
    await sbx.write_file("m", b"new")
    # The links are still links, and the files behind them changed.
    out = await _sh(sbx, "test -L srclink && test -L m && cat src/new.py src/mod.py")
    assert out == "through the linknew"


async def test_write_file_does_not_follow_a_symlink_out_of_the_workspace(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    await _sh(sbx, "ln -s /etc outlink && ln -s /etc/hostname hostlink")
    with pytest.raises(PermissionError):
        await sbx.write_file("outlink/evil", b"x")
    with pytest.raises(PermissionError):
        await sbx.write_file("hostlink", b"x")
    assert (await sbx.exec("test -e /etc/evil", timeout_s=10)).exit_code != 0
    assert (await _sh(sbx, "test -L hostlink && echo link")).strip() == "link"


async def test_same_size_edit_is_seen_by_python(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    run = "python -c 'import calc; print(calc.add(2, 3))'"
    await sbx.write_file("calc.py", b"def add(a, b):\n    return a - b\n")
    assert (await _sh(sbx, run)).strip() == "-1"
    # Same byte length, so a stale .pyc would still look valid to Python.
    await sbx.write_file("calc.py", b"def add(a, b):\n    return a + b\n")
    assert (await _sh(sbx, run)).strip() == "5"
    assert (await _sh(sbx, "find . -name '*.pyc' | wc -l")).strip() == "0"


async def test_python_does_not_write_bytecode(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    out = await _sh(sbx, "python -c 'import sys; print(sys.dont_write_bytecode)'")
    assert out.strip() == "True"
    assert ENV_ALLOWLIST["PYTHONDONTWRITEBYTECODE"] == "1"


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
