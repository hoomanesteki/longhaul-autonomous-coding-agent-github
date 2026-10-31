"""The checks behind DockerSandboxManager.create(mounts=...) (LH-205).

The checks run before a container exists, so a wrong mount stops with a clear error and never
leaves a half-built sandbox behind.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from longhaul.sandbox.docker_manager import bind_mounts

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo.git"
    path.mkdir()
    return path


def test_no_mounts_adds_nothing() -> None:
    assert bind_mounts(None) == {}
    assert bind_mounts({}) == {}


def test_a_host_folder_becomes_a_read_write_bind(repo: Path) -> None:
    assert bind_mounts({str(repo): "/ckpt"}) == {
        str(repo.resolve()): {"bind": "/ckpt", "mode": "rw"}
    }


def test_a_symlinked_host_path_is_resolved_first(tmp_path: Path, repo: Path) -> None:
    # Docker Desktop shares real paths. /tmp and /var are links on a Mac.
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert list(bind_mounts({str(link): "/ckpt"})) == [str(repo.resolve())]


def test_two_mounts_are_fine_when_they_do_not_overlap(tmp_path: Path, repo: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    binds = bind_mounts({str(repo): "/ckpt", str(other): "/data"})
    assert {entry["bind"] for entry in binds.values()} == {"/ckpt", "/data"}


@pytest.mark.parametrize("host", ["relative/dir", "", "~/repo.git"])
def test_the_host_path_must_be_absolute(host: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        bind_mounts({host: "/ckpt"})


def test_the_host_path_must_exist_so_docker_does_not_create_it_as_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        bind_mounts({str(tmp_path / "missing"): "/ckpt"})


def test_only_folders_can_be_mounted(tmp_path: Path) -> None:
    file = tmp_path / "file"
    file.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        bind_mounts({str(file): "/ckpt"})


def test_the_host_root_is_never_mounted() -> None:
    with pytest.raises(ValueError, match="root"):
        bind_mounts({"/": "/ckpt"})


@pytest.mark.parametrize("target", ["ckpt", "/", "/ckpt/", "/ckpt/../etc", "/a//b", ""])
def test_the_container_path_must_be_absolute_and_plain(repo: Path, target: str) -> None:
    with pytest.raises(ValueError, match="container path"):
        bind_mounts({str(repo): target})


@pytest.mark.parametrize(
    "target", ["/workspace", "/workspace/sub", "/tmp", "/tmp/x", "/home/agent", "/home/agent/x"]
)
def test_a_mount_cannot_sit_on_the_writable_places_of_the_sandbox(repo: Path, target: str) -> None:
    with pytest.raises(ValueError, match="container path"):
        bind_mounts({str(repo): target})


def test_a_mount_cannot_hide_the_writable_places_of_the_sandbox(repo: Path) -> None:
    with pytest.raises(ValueError, match="container path"):
        bind_mounts({str(repo): "/home"})


def test_mounts_cannot_overlap(tmp_path: Path, repo: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="overlap"):
        bind_mounts({str(repo): "/ckpt", str(other): "/ckpt/inner"})
    with pytest.raises(ValueError, match="overlap"):
        bind_mounts({str(repo): "/ckpt", str(other): "/ckpt"})


def test_one_host_folder_cannot_be_mounted_twice(tmp_path: Path, repo: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ValueError, match="twice"):
        bind_mounts({str(repo): "/one", str(link): "/two"})
