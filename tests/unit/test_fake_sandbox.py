import os
import shutil
import sys
import tempfile
from pathlib import Path

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


async def test_exec_finds_tools_in_the_workspace_venv_first(sbx: FakeSandbox) -> None:
    # Same rule as the Docker sandbox: <workspace>/.venv/bin leads PATH.
    await sbx.write_file(".venv/bin/hello-tool", b"#!/bin/sh\necho from-venv\n")
    await sbx.write_file(".venv/bin/ls", b"#!/bin/sh\necho shadowed\n")
    await sbx.exec("chmod +x .venv/bin/hello-tool .venv/bin/ls", timeout_s=5)
    assert (await sbx.exec("hello-tool", timeout_s=5)).stdout.strip() == "from-venv"
    assert (await sbx.exec("ls", timeout_s=5)).stdout.strip() == "shadowed"
    # The command sees the workspace as /workspace, as it does in Docker.
    assert (await sbx.exec("echo $PATH", timeout_s=5)).stdout.startswith("/workspace/.venv/bin:")


async def test_exec_still_works_without_a_venv(sbx: FakeSandbox) -> None:
    r = await sbx.exec("command -v sh", timeout_s=5)
    assert r.exit_code == 0 and r.stdout.strip()


async def test_file_roundtrip_nested_and_absolute(sbx: FakeSandbox) -> None:
    data = bytes(range(256))
    await sbx.write_file("a/b/c.bin", data)
    assert await sbx.read_file("a/b/c.bin") == data
    assert await sbx.read_file("/workspace/a/b/c.bin") == data


async def test_path_escape_rejected(sbx: FakeSandbox) -> None:
    with pytest.raises(ValueError):
        await sbx.write_file("../evil", b"x")


# ---------- paths: the same rules as the Docker sandbox ----------


async def test_paths_outside_the_workspace_are_rejected_for_reads_and_writes(
    sbx: FakeSandbox,
) -> None:
    await sbx.write_file("good.txt", b"ok")
    # Docker raises PermissionError for these and the fake used to raise ValueError
    # or quietly treat them as paths below the root. Both types must work.
    for path in ("../../etc/passwd", "/etc/passwd", "/workspace/../etc/passwd", "//etc/hosts"):
        with pytest.raises(PermissionError):
            await sbx.read_file(path)
        with pytest.raises(ValueError):
            await sbx.write_file(path, b"x")
    assert await sbx.read_file("good.txt") == b"ok"
    assert not (sbx.root / "etc").exists()


async def test_a_symlink_that_leaves_the_workspace_is_rejected_for_reads(sbx: FakeSandbox) -> None:
    outside = tempfile.mkdtemp(prefix="lh-outside-")
    try:
        (Path(outside) / "secret.txt").write_text("secret")
        await sbx.exec(
            f"ln -s {outside} outlink && ln -s {outside}/secret.txt filelink", timeout_s=5
        )
        for path in ("outlink/secret.txt", "filelink"):
            with pytest.raises(PermissionError):
                await sbx.read_file(path)
    finally:
        shutil.rmtree(outside, ignore_errors=True)


async def test_read_file_errors_match_docker(sbx: FakeSandbox) -> None:
    await sbx.write_file("src/pkg/a.py", b"x")
    await sbx.write_file("README.md", b"keep")
    with pytest.raises(FileNotFoundError):
        await sbx.read_file("missing.txt")
    with pytest.raises(IsADirectoryError):
        await sbx.read_file("src")
    with pytest.raises(IsADirectoryError):
        await sbx.read_file(".")
    with pytest.raises(NotADirectoryError):
        await sbx.read_file("README.md/x")


# ---------- exec and the file API agree on where /workspace is ----------


async def test_exec_sees_what_write_file_wrote_at_the_workdir(sbx: FakeSandbox) -> None:
    assert sbx.workdir == "/workspace"
    await sbx.write_file(f"{sbx.workdir}/src/x.txt", b"hello")
    r = await sbx.exec(f"cat {sbx.workdir}/src/x.txt", timeout_s=5)
    assert (r.exit_code, r.stdout) == (0, "hello")


async def test_pwd_and_realpath_answer_with_the_workdir(sbx: FakeSandbox) -> None:
    await sbx.write_file("src/x.txt", b"hello")
    r = await sbx.exec("pwd; realpath src/x.txt; cd src && pwd", timeout_s=5)
    lines = r.stdout.split()
    assert lines == ["/workspace", "/workspace/src/x.txt", "/workspace/src"]
    assert all(line.startswith(sbx.workdir) for line in lines)


async def test_commands_can_use_the_workdir_by_name(sbx: FakeSandbox) -> None:
    git = "git -c user.name=t -c user.email=t@example.com"
    script = (
        "cd /workspace && git init -q && echo hi > f.txt && "
        f"git -C /workspace add -A && {git} -C /workspace commit -qm first && "
        "git -C /workspace log --oneline | wc -l"
    )
    r = await sbx.exec(script, timeout_s=60)
    assert r.exit_code == 0, r.stderr
    assert r.stdout.strip() == "1"
    assert (await sbx.read_file("f.txt")) == b"hi\n"


async def test_exec_env_values_may_name_the_workdir(sbx: FakeSandbox) -> None:
    r = await sbx.exec("ls -d $WHERE && echo $WHERE", timeout_s=5, env={"WHERE": "/workspace"})
    assert (r.exit_code, r.stdout.split()) == (0, ["/workspace", "/workspace"])


# ---------- write_file does what the Docker sandbox does ----------


async def test_write_file_refuses_a_directory(sbx: FakeSandbox) -> None:
    await sbx.write_file("pkg/a.py", b"x")
    for path in ("pkg", "/workspace/pkg", "."):
        with pytest.raises(IsADirectoryError):
            await sbx.write_file(path, b"oops")
    assert await sbx.read_file("pkg/a.py") == b"x"


async def test_write_file_refuses_a_parent_that_is_a_file(sbx: FakeSandbox) -> None:
    await sbx.write_file("README.md", b"keep")
    # One level and two levels down used to raise different errors.
    for path in ("README.md/x", "README.md/deeper/x"):
        with pytest.raises(NotADirectoryError):
            await sbx.write_file(path, b"z")
    assert await sbx.read_file("README.md") == b"keep"


async def test_write_file_keeps_the_mode_of_an_existing_file(sbx: FakeSandbox) -> None:
    await sbx.write_file("run.sh", b"#!/bin/sh\necho one\n")
    await sbx.exec("chmod 755 run.sh", timeout_s=5)
    await sbx.write_file("run.sh", b"#!/bin/sh\necho two\n")
    assert (await sbx.exec("./run.sh", timeout_s=5)).stdout.strip() == "two"


async def test_write_file_follows_a_symlink_inside_the_workspace(sbx: FakeSandbox) -> None:
    await sbx.write_file("src/mod.py", b"old")
    await sbx.exec("ln -s src srclink && ln -s src/mod.py m", timeout_s=5)
    await sbx.write_file("srclink/new.py", b"through the link")
    await sbx.write_file("m", b"new")
    assert await sbx.read_file("src/new.py") == b"through the link"
    assert await sbx.read_file("src/mod.py") == b"new"


async def test_write_file_does_not_follow_a_symlink_out_of_the_workspace(sbx: FakeSandbox) -> None:
    outside = tempfile.mkdtemp(prefix="lh-outside-")
    try:
        await sbx.exec(f"ln -s {outside} outlink", timeout_s=5)
        with pytest.raises(PermissionError):
            await sbx.write_file("outlink/evil", b"x")
        assert os.listdir(outside) == []
    finally:
        shutil.rmtree(outside, ignore_errors=True)


async def test_bytecode_is_never_written(sbx: FakeSandbox) -> None:
    r = await sbx.exec("echo $PYTHONDONTWRITEBYTECODE", timeout_s=5)
    assert r.stdout.strip() == "1"


async def test_same_size_edit_is_seen_by_python(sbx: FakeSandbox) -> None:
    # A stale .pyc made the second run print -1 in most back-to-back runs, because
    # the edit lands in the same second as the first import and keeps the byte size.
    run = f"{sys.executable} -c 'import calc; print(calc.add(2, 3))'"
    for _ in range(5):
        await sbx.write_file("calc.py", b"def add(a, b):\n    return a - b\n")
        assert (await sbx.exec(run, timeout_s=30)).stdout.strip() == "-1"
        await sbx.write_file("calc.py", b"def add(a, b):\n    return a + b\n")
        assert (await sbx.exec(run, timeout_s=30)).stdout.strip() == "5"


def test_names() -> None:
    assert container_name("t1", "abcd1234") == "lh-sbx-t1-abcd1234"
    assert len(container_name("t1")) == len("lh-sbx-t1-") + 8
    assert volume_name("t1") == "lh-ws-t1"


def test_env_only_allowlist_plus_explicit() -> None:
    assert merge_env(None) == ENV_ALLOWLIST
    merged = merge_env({"X": "1", "HOME": "/h"})
    assert merged["X"] == "1" and merged["HOME"] == "/h"
    assert set(ENV_ALLOWLIST) == {
        "PATH",
        "HOME",
        "LANG",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
    }


def test_path_starts_with_the_workspace_venv() -> None:
    # Packages go into /workspace/.venv because the root filesystem is read-only.
    # Putting its bin first means installed tools work without activating it.
    entries = ENV_ALLOWLIST["PATH"].split(":")
    assert entries[0] == "/workspace/.venv/bin"
    assert merge_env(None)["PATH"].startswith("/workspace/.venv/bin:")
    assert merge_env({"X": "1"})["PATH"].startswith("/workspace/.venv/bin:")
    # The image's own directories still follow, in their original order.
    assert entries[1:] == [
        "/usr/local/bin",
        "/usr/local/sbin",
        "/usr/local/bin",
        "/usr/sbin",
        "/usr/bin",
        "/sbin",
        "/bin",
    ]


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
