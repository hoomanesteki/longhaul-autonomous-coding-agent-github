import pytest

from longhaul.sandbox.docker_manager import (
    ENV_ALLOWLIST,
    build_tar,
    cap_output,
    container_name,
    merge_env,
    mount_for,
    resolve_path,
    volume_name,
)
from longhaul.sandbox.local_manager import FakeSandbox

pytestmark = [pytest.mark.unit, pytest.mark.phase1]


@pytest.fixture
async def sbx():
    s = FakeSandbox()
    yield s
    await s.destroy()


async def test_exec_captures_streams(sbx: FakeSandbox) -> None:
    r = await sbx.exec("echo hi; echo oops >&2; exit 3", timeout_s=5)
    assert (r.stdout.strip(), r.stderr.strip(), r.exit_code) == ("hi", "oops", 3)
    assert not r.timed_out


async def test_exec_timeout(sbx: FakeSandbox) -> None:
    r = await sbx.exec("sleep 30", timeout_s=1)
    assert r.timed_out
    assert r.duration_ms < 5000


async def test_exec_env_passthrough(sbx: FakeSandbox) -> None:
    r = await sbx.exec("echo $FOO", timeout_s=5, env={"FOO": "bar"})
    assert r.stdout.strip() == "bar"


async def test_file_roundtrip_nested_and_absolute(sbx: FakeSandbox) -> None:
    data = bytes(range(256))
    await sbx.write_file("a/b/c.bin", data)
    assert await sbx.read_file("a/b/c.bin") == data
    assert await sbx.read_file("/workspace/a/b/c.bin") == data


async def test_path_escape_rejected(sbx: FakeSandbox) -> None:
    with pytest.raises(ValueError):
        await sbx.write_file("../evil", b"x")


def test_names() -> None:
    assert container_name("t1", "abcd1234") == "lh-sbx-t1-abcd1234"
    assert len(container_name("t1")) == len("lh-sbx-t1-") + 8
    assert volume_name("t1") == "lh-ws-t1"


def test_env_only_allowlist_plus_explicit() -> None:
    assert merge_env(None) == ENV_ALLOWLIST
    merged = merge_env({"X": "1", "HOME": "/h"})
    assert merged["X"] == "1" and merged["HOME"] == "/h"
    assert set(ENV_ALLOWLIST) == {"PATH", "HOME", "LANG", "PYTHONUNBUFFERED"}


def test_cap_output() -> None:
    assert cap_output(None) == ""
    assert cap_output(b"abc") == "abc"
    capped = cap_output(b"a" * 20, limit=10)
    assert capped.startswith("a" * 10) and "truncated" in capped


def test_mount_for() -> None:
    assert mount_for("/workspace/a/b") == "/workspace"
    assert mount_for("/tmp/x") == "/tmp"
    with pytest.raises(PermissionError):
        mount_for("/etc/passwd")


def test_resolve_path() -> None:
    assert resolve_path("x/y") == "/workspace/x/y"
    assert resolve_path("/tmp/z") == "/tmp/z"
    assert resolve_path("a/../b") == "/workspace/b"


def test_build_tar_owner_and_dirs() -> None:
    import io
    import tarfile

    raw = build_tar("d/f.txt", b"hey")
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        names = [m.name for m in tar.getmembers()]
        assert names == ["d", "d/f.txt"]
        assert all(m.uid == 10001 and m.gid == 10001 for m in tar.getmembers())
