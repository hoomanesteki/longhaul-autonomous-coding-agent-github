"""The sandbox image of an instance and the copy of its repo (LH-902).

The Docker side is a fake that records the commands, and the tar stream is made in memory. The
build of a real image from a real instance image runs in tests/integration/test_swebench_adapter.py.
"""

from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from eval.swebench import images

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

BASE_IMAGE = "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"


@dataclass(frozen=True)
class FakeInstance:
    instance_id: str = "django__django-11099"
    image: str = BASE_IMAGE
    base_commit: str = "a" * 40


# ---------- the Dockerfile and the tag ----------
def test_the_dockerfile_starts_from_the_instance_image_as_root() -> None:
    lines = images.derived_dockerfile(BASE_IMAGE).splitlines()

    assert lines[0] == f"FROM {BASE_IMAGE}"
    assert "USER root" in lines


def test_the_dockerfile_makes_the_user_the_sandbox_manager_runs_commands_as() -> None:
    text = images.derived_dockerfile(BASE_IMAGE)

    # DockerSandboxManager starts every command as user "agent" with uid 10001.
    assert "--uid 10001" in text
    assert "agent" in text


def test_the_repo_moves_to_workspace_and_testbed_stays_as_a_link() -> None:
    text = images.derived_dockerfile(BASE_IMAGE)

    # The task volume is mounted at /workspace, and the conda environment of the instance
    # points at /testbed, so the repo lives at the first path and the second leads there.
    assert "mv /testbed /workspace" in text
    assert "ln -s /workspace /testbed" in text
    assert "chown -R agent:agent /workspace" in text


def test_the_conda_environment_leads_the_path_of_every_command_through_the_venv_link() -> None:
    text = images.derived_dockerfile(BASE_IMAGE)

    # The sandbox puts /workspace/.venv/bin first on PATH. The link makes that the testbed env.
    assert f"ln -s {images.CONDA_ENV} /workspace/.venv" in text
    assert images.CONDA_ENV == "/opt/miniconda3/envs/testbed"


def test_the_dockerfile_gives_the_supervisor_a_python_and_the_search_tool_ripgrep() -> None:
    text = images.derived_dockerfile(BASE_IMAGE)

    assert "/usr/local/bin/python3" in text
    assert "ripgrep" in text


def test_the_dockerfile_does_not_start_the_container_as_root() -> None:
    lines = images.derived_dockerfile(BASE_IMAGE).splitlines()

    assert lines.index("USER agent") > lines.index("USER root")
    assert lines[-1] != "USER root"


def test_the_dockerfile_is_the_same_every_time() -> None:
    assert images.derived_dockerfile(BASE_IMAGE) == images.derived_dockerfile(BASE_IMAGE)


def test_the_tag_names_the_instance_and_follows_the_dockerfile() -> None:
    tag = images.derived_tag("django__django-11099", BASE_IMAGE)

    assert tag.startswith("longhaul-swebench:django__django-11099-")
    assert tag == tag.lower()
    assert images.derived_tag("django__django-11099", BASE_IMAGE) == tag
    # Another base image means another Dockerfile, so another tag: a cached image never goes stale.
    assert images.derived_tag("django__django-11099", BASE_IMAGE + "x") != tag
    assert images.derived_tag("django__django-11100", BASE_IMAGE) != tag


def test_a_tag_is_a_valid_docker_tag_for_every_kind_of_instance_id() -> None:
    for instance_id in (
        "sympy__sympy-20590",
        "scikit-learn__scikit-learn-13142",
        "psf__requests-1",
    ):
        tag = images.derived_tag(instance_id, BASE_IMAGE)
        _, _, version = tag.partition(":")
        assert len(version) <= 128
        assert all(c.isalnum() or c in "_.-" for c in version)


# ---------- the copy of the repo ----------
def tar_of(files: dict[str, bytes], *, links: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            link = tarfile.TarInfo(name)
            link.type = tarfile.SYMTYPE
            link.linkname = target
            tar.addfile(link)
    return buffer.getvalue()


def test_the_files_of_the_stream_are_written_below_the_folder(tmp_path: Path) -> None:
    stream = io.BytesIO(
        tar_of({"pkg/core.py": b"x = 1\n", "run.sh": b"echo hi\n", "README.md": b"hello\n"})
    )

    count = images.extract_tree(stream, tmp_path)

    assert count == 3
    assert (tmp_path / "pkg" / "core.py").read_bytes() == b"x = 1\n"
    assert (tmp_path / "run.sh").stat().st_mode & 0o111
    assert (tmp_path / "README.md").read_bytes() == b"hello\n"


def test_a_name_that_climbs_out_of_the_folder_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "repo"
    folder.mkdir()
    stream = io.BytesIO(tar_of({"../escape.txt": b"no"}))

    with pytest.raises(images.ImageError, match="escape"):
        images.extract_tree(stream, folder)

    assert not (tmp_path / "escape.txt").exists()


def test_an_absolute_name_is_refused(tmp_path: Path) -> None:
    stream = io.BytesIO(tar_of({"/etc/cron.d/x": b"no"}))

    with pytest.raises(images.ImageError, match="/etc/cron.d/x"):
        images.extract_tree(stream, tmp_path)


def test_a_link_that_leaves_the_folder_is_not_written(tmp_path: Path) -> None:
    folder = tmp_path / "repo"
    folder.mkdir()
    stream = io.BytesIO(tar_of({"a.txt": b"a"}, links={"out": "../../outside"}))

    images.extract_tree(stream, folder)

    # The sandbox's file API cannot make links either (agent/workspace.py), so links are skipped.
    assert (folder / "a.txt").exists()
    assert not (folder / "out").exists() and not (folder / "out").is_symlink()


def test_an_empty_stream_is_an_error() -> None:
    with pytest.raises(images.ImageError, match="no files"):
        images.extract_tree(io.BytesIO(b""), Path("/nonexistent"))


# ---------- what runs in the instance image ----------
class FakeDocker:
    def __init__(
        self,
        *,
        present: set[str] | None = None,
        head: str = "a" * 40,
        files: dict[str, bytes] | None = None,
    ) -> None:
        self.present = set(present or ())
        self.head = head
        self.files = files or {"pkg/core.py": b"x = 1\n"}
        self.calls: list[tuple[str, ...]] = []

    def has_image(self, name: str) -> bool:
        self.calls.append(("has", name))
        return name in self.present

    def pull(self, name: str) -> None:
        self.calls.append(("pull", name))
        self.present.add(name)

    def build(self, tag: str, dockerfile: str) -> None:
        self.calls.append(("build", tag))
        assert dockerfile.startswith("FROM ")
        self.present.add(tag)

    def read_head(self, image: str) -> str:
        self.calls.append(("head", image))
        return self.head

    def export_tree(self, image: str) -> io.BytesIO:
        self.calls.append(("export", image))
        return io.BytesIO(tar_of(self.files))


def test_preparing_an_instance_pulls_builds_and_exports_once(tmp_path: Path) -> None:
    docker = FakeDocker()
    tools = images.DockerImages(docker)

    tag = tools.prepare(FakeInstance(), tmp_path)

    assert tag == images.derived_tag("django__django-11099", BASE_IMAGE)
    assert [c[0] for c in docker.calls if c[0] in ("pull", "build", "export")] == [
        "pull",
        "build",
        "export",
    ]
    assert (tmp_path / "pkg" / "core.py").read_bytes() == b"x = 1\n"


def test_an_image_that_is_there_is_neither_pulled_nor_built_again(tmp_path: Path) -> None:
    tag = images.derived_tag("django__django-11099", BASE_IMAGE)
    docker = FakeDocker(present={BASE_IMAGE, tag})

    images.DockerImages(docker).prepare(FakeInstance(), tmp_path)

    assert [c[0] for c in docker.calls if c[0] in ("pull", "build")] == []


def test_an_instance_image_at_another_commit_is_refused(tmp_path: Path) -> None:
    docker = FakeDocker(present={BASE_IMAGE}, head="b" * 40)

    with pytest.raises(images.ImageError, match="base commit"):
        images.DockerImages(docker).prepare(FakeInstance(), tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_a_failed_pull_says_which_image_and_why() -> None:
    class Failing(FakeDocker):
        def pull(self, name: str) -> None:
            raise images.ImageError(f"cannot pull {name}: no matching manifest")

    with pytest.raises(images.ImageError, match="no matching manifest"):
        images.DockerImages(Failing()).prepare(FakeInstance(), Path("/unused"))
