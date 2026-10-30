"""Pure helpers of the Docker sandbox. Nothing here talks to Docker."""

import io
import tarfile
import time

import pytest

from longhaul.sandbox.docker_manager import AGENT_UID, build_tar

pytestmark = [pytest.mark.unit, pytest.mark.phase1]


def members(raw: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return tar.getmembers()


# ---------- build_tar ----------


def test_build_tar_stamps_files_and_directories_with_the_current_time() -> None:
    before = int(time.time())
    raw = build_tar("d/f.txt", b"x")
    after = time.time()
    found = members(raw)
    assert [m.name for m in found] == ["d", "d/f.txt"]
    # Python trusts a .pyc when the source mtime and size match, and mtime 0 never changes.
    assert all(before <= m.mtime <= after for m in found)


def test_build_tar_uses_the_given_file_mode_and_keeps_directories_at_755() -> None:
    d, f = members(build_tar("d/run.sh", b"#!/bin/sh\n", mode=0o755))
    assert (d.mode, f.mode) == (0o755, 0o755)
    d, f = members(build_tar("d/plain.txt", b"x"))
    assert (d.mode, f.mode) == (0o755, 0o644)


def test_build_tar_without_parents_holds_just_the_file() -> None:
    (only,) = members(build_tar("f.txt", b"hey"))
    assert only.name == "f.txt" and only.isfile() and only.size == 3
    assert only.uid == only.gid == AGENT_UID
