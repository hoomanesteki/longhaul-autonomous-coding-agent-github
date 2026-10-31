import asyncio
import json
import os
import subprocess
import time
import tracemalloc
import uuid
from collections.abc import AsyncIterator
from typing import Any

import docker
import pytest
from docker.errors import NotFound
from docker.utils.proxy import ProxyConfig

from longhaul.contracts import Sandbox, SandboxLimits
from longhaul.sandbox.common import OUTPUT_CAP
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
            IMAGE,
            "python",
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


def _inspect(container_id: str) -> dict[str, Any]:
    return docker.from_env().api.inspect_container(container_id)


def _networks(container_id: str) -> set[str]:
    """The networks Docker says the container is attached to."""
    return set(_inspect(container_id)["NetworkSettings"]["Networks"])


async def _routes(sbx: Sandbox) -> set[str]:
    """The interfaces the container has an IPv4 route through, read from inside it.

    No routes means no way out. The interface list is no use for this: the Docker VM's
    kernel adds tunnel placeholders (gre0, erspan0...) to every network namespace.
    """
    return set((await _sh(sbx, "awk 'NR>1 {print $1}' /proc/net/route")).split())


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
    # Checked from the inside and from Docker's side, so it holds on a machine with no
    # internet too. A failed download alone proves nothing: the host may just be offline.
    assert _networks(sbx.id) == set()
    assert await _routes(sbx) == set()
    assert (await sbx.exec(PYPI, timeout_s=20)).exit_code != 0


async def test_network_is_attached_when_enabled(
    manager: DockerSandboxManager, task_id: str
) -> None:
    # The control for the test above: the same two checks see a network when there is one.
    sbx = await _make(manager, task_id, network=True)
    assert _networks(sbx.id) == {"bridge"}
    assert await _routes(sbx) == {"eth0"}


async def test_network_toggle(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id, network=True)
    if _online():
        assert (await sbx.exec(PYPI, timeout_s=20)).exit_code == 0
    else:
        print("no outbound network on this machine; the structural checks still run")
    await manager.set_network(sbx.id, False)
    assert _networks(sbx.id) == set() and await _routes(sbx) == set()
    assert (await sbx.exec(PYPI, timeout_s=20)).exit_code != 0
    await manager.set_network(sbx.id, True)
    assert _networks(sbx.id) == {"bridge"} and await _routes(sbx) == {"eth0"}
    await manager.set_network(sbx.id, False)  # idempotent in both directions
    await manager.set_network(sbx.id, False)
    assert _networks(sbx.id) == set() and await _routes(sbx) == set()
    assert (await sbx.exec(PYPI, timeout_s=20)).exit_code != 0


async def test_timeout_kills(manager: DockerSandboxManager, task_id: str) -> None:
    sbx = await _make(manager, task_id)
    r = await sbx.exec("sleep 30", timeout_s=2)
    assert r.timed_out
    assert r.duration_ms < 5000


async def test_exec_keeps_the_head_and_the_tail_of_big_output(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    # 24 MB on stdout and 3 MB on stderr, with a marker at each end of each stream.
    script = (
        "echo HEAD-OUT; head -c 24000000 /dev/zero | tr '\\0' a; echo; echo TAIL-OUT; "
        "echo HEAD-ERR >&2; head -c 3000000 /dev/zero | tr '\\0' b >&2; echo >&2; echo TAIL-ERR >&2"
    )
    tracemalloc.start()
    try:
        r = await sbx.exec(script, timeout_s=120)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert r.exit_code == 0 and not r.timed_out
    # The tools cut again to 8,000 plus 12,000 characters, so the real end must survive.
    assert r.stdout.startswith("HEAD-OUT\n") and r.stdout.endswith("TAIL-OUT\n")
    assert r.stderr.startswith("HEAD-ERR\n") and r.stderr.endswith("TAIL-ERR\n")
    assert "bytes omitted" in r.stdout and "bytes omitted" in r.stderr
    assert OUTPUT_CAP < len(r.stdout) < OUTPUT_CAP + 100
    # The worker kept about the cap per stream, not the 27 MB the command printed.
    # Buffering it all first took 1.1 GB for 20 MB of output.
    assert peak < 12_000_000, peak


async def test_a_flood_of_output_ends_at_the_timeout_with_bounded_memory(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    tracemalloc.start()
    try:
        started = time.monotonic()
        r = await sbx.exec("yes", timeout_s=3)
        elapsed = time.monotonic() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert r.timed_out and r.exit_code == 137
    assert elapsed < 30
    assert r.stdout.startswith("y\ny\n") and r.stdout.endswith("y\n")
    assert peak < 12_000_000, peak


async def test_exec_rejects_a_timeout_that_is_not_positive(
    manager: DockerSandboxManager, task_id: str
) -> None:
    # timeout reads 0 as "no limit", so a command could have run for ever.
    sbx = await _make(manager, task_id)
    for bad in (0, -5):
        with pytest.raises(ValueError, match="timeout_s"):
            await sbx.exec("true", timeout_s=bad)
    assert (await _sh(sbx, "echo still fine")).strip() == "still fine"


# A daemon: the first child starts a new session, and the grandchild is adopted by init
# once the child exits. The newlines are real ones because python reads the program
# from its argument.
DOUBLE_FORK = (
    "python3 -c 'import os\n"
    "if os.fork() == 0:\n"
    " os.setsid()\n"
    " if os.fork() == 0:\n"
    '  os.execv("/bin/sleep", ["sleep", "65"])\n'
    " os._exit(0)'"
)


async def test_exec_kills_what_a_command_leaves_running(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    leftovers = (
        "sleep 61 & "  # a plain background job
        "nohup sleep 62 >/dev/null 2>&1 & "
        "setsid -f sleep 63; "  # its own session, so the group kill cannot reach it
        "env -i /usr/bin/setsid -f /bin/sleep 64; "  # and a scrubbed environment
        f"{DOUBLE_FORK}; "  # a daemon: forks twice and is adopted by init
        "echo started"
    )
    r = await sbx.exec(leftovers, timeout_s=60)
    # An empty stderr shows the daemon snippet ran: a syntax error in it would print here.
    assert (r.exit_code, r.stdout.strip(), r.stderr, r.timed_out) == (0, "started", "", False)
    assert r.duration_ms < 15_000  # the sleepers do not hold the call open
    left = await sbx.exec("pgrep -af '[s]leep 6'; echo rc=$?", timeout_s=15)
    assert left.stdout.strip() == "rc=1", left.stdout
    # The container's idle process is not the command's, so it survives, and so does exec.
    idle = await _sh(sbx, "ps -eo args | grep -c '^sleep infinity$'")
    assert idle.strip() == "1"


async def test_a_timeout_also_kills_children_that_left_the_process_group(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    r = await sbx.exec("setsid -f sleep 66; sleep 30", timeout_s=2)
    assert r.timed_out and r.exit_code == 137
    assert r.duration_ms < 10_000
    left = await sbx.exec("pgrep -af '[s]leep 6'; echo rc=$?", timeout_s=15)
    assert left.stdout.strip() == "rc=1", left.stdout


async def test_one_exec_does_not_kill_the_children_of_another(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    slow = asyncio.create_task(sbx.exec("sleep 6 & wait; echo slow-done", timeout_s=60))
    await asyncio.sleep(1.5)
    quick = await sbx.exec("setsid -f sleep 67; echo quick-done", timeout_s=60)
    assert quick.stdout.strip() == "quick-done"
    assert (await slow).stdout.strip() == "slow-done"


async def test_pid_1_is_an_init_that_reaps_orphans(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await _make(manager, task_id)
    assert _inspect(sbx.id)["HostConfig"].get("Init") is True
    # A process whose parent exits is adopted by PID 1. This one is started outside
    # exec(), so no supervisor owns it and reaping it is up to PID 1. With `sleep
    # infinity` there it stayed a zombie for the life of the container.
    api = docker.from_env().api
    created = api.exec_create(sbx.id, ["/bin/sh", "-c", "sleep 1 & exit 0"], user="agent")
    api.exec_start(created["Id"], detach=True)
    await asyncio.sleep(3)
    zombies = await _sh(sbx, "ps -eo stat= | grep -c '^Z' || true")
    assert zombies.strip() == "0"


async def test_orphans_do_not_pile_up_against_the_pid_limit(
    manager: DockerSandboxManager, task_id: str
) -> None:
    # Every round leaves 15 orphans behind. Nothing used to reap them, so they stayed
    # zombies holding a pid each until fork failed and every command died with 125.
    sbx = await manager.create(task_id, IMAGE, SandboxLimits(pids=48), network=False)
    burst = "for i in $(seq 15); do (sleep 0.2 &); done; sleep 0.5; echo done"
    for _ in range(6):
        r = await sbx.exec(burst, timeout_s=30)
        assert (r.exit_code, r.stdout.strip()) == (0, "done"), r.stderr
    assert (await _sh(sbx, "ps -eo stat= | grep -c '^Z' || true")).strip() == "0"
    assert int((await _sh(sbx, "ps -e --no-headers | wc -l")).strip()) < 10


async def test_repeated_timeouts_leave_nothing_behind(
    manager: DockerSandboxManager, task_id: str
) -> None:
    sbx = await manager.create(task_id, IMAGE, SandboxLimits(pids=16), network=False)
    for _ in range(9):  # bash and sleep leaked on every timeout: 7 rounds already filled 16 pids
        assert (await sbx.exec("sleep 30; true", timeout_s=1)).timed_out
    assert (await _sh(sbx, "echo alive")).strip() == "alive"
    assert int((await _sh(sbx, "ps -e --no-headers | wc -l")).strip()) < 10


async def test_host_proxy_settings_do_not_reach_the_container(
    manager: DockerSandboxManager, task_id: str
) -> None:
    # docker-py copies the proxies block of ~/.docker/config.json into the env of every
    # container it creates, credentials included, unless told not to (I3).
    proxy = "http://corp-user:s3cret-pass@proxy.corp.example:3128"
    proxies = ProxyConfig(http=proxy, https=proxy, ftp=None, no_proxy="localhost")
    assert any("s3cret" in e for e in proxies.inject_proxy_environment([]))  # the control
    client = docker.from_env()
    client.api._proxy_configs = proxies  # what a config.json with a proxies block sets up
    sbx = await _make(DockerSandboxManager(client), task_id)

    cfg_env = _inspect(sbx.id)["Config"]["Env"]
    assert not [e for e in cfg_env if "proxy" in e.lower()], cfg_env
    inside = (await sbx.exec("env", timeout_s=10)).stdout
    assert "proxy" not in inside.lower() and "s3cret" not in inside
    # The check that already existed: nothing outside the allowlist and the image's own.
    image_env = {e.split("=", 1)[0] for e in client.images.get(IMAGE).attrs["Config"]["Env"]}
    allowed = set(ENV_ALLOWLIST) | image_env | {"HOSTNAME"}
    assert {e.split("=", 1)[0] for e in cfg_env} <= allowed


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
    assert host.get("Init") is True
    assert host["PidsLimit"] == SandboxLimits().pids
    assert host["Memory"] == SandboxLimits().memory_mb * 1024 * 1024
    assert host["NanoCpus"] == 2_000_000_000
    assert info["Config"]["Labels"]["longhaul.task_id"] == task_id
    assert info["Name"].startswith(f"/lh-sbx-{task_id}-")
