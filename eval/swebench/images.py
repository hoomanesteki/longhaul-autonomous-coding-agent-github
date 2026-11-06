"""The sandbox image of a SWE-bench instance and the copy of its repo (LH-902).

An instance image from the harness holds the repo at /testbed, a conda environment named
`testbed` with the dependencies installed, and nothing else the sandbox needs. The sandbox
manager (longhaul/sandbox/docker_manager.py) expects four things of an image that the instance
image does not give, so each instance gets a thin image of its own on top:

* a user `agent` with uid 10001, because every command runs as that user;
* the repo at /workspace, because the task volume is mounted there and the tools work there.
  The conda environment points at /testbed (an editable install records that path), so /testbed
  stays as a link to /workspace and the install keeps working. The git history is removed, since
  the clone also holds the commits that came after the base commit;
* the testbed environment first on PATH. The sandbox already puts /workspace/.venv/bin first, so
  /workspace/.venv is a link to the environment and `python`, `pytest` and `pip` are the ones of
  the instance without any change to the sandbox manager;
* `/usr/local/bin/python3` for the supervisor that wraps every command, and ripgrep for the
  search tool.

The repo itself reaches the worker the way a local folder does. `export_tree` writes the files of
the instance's working tree to a folder on the host, and the folder becomes the `local:` repo of
the task. The worker commits it as the base of the checkpoint repo, so the patch of the agent is a
diff against exactly the files the instance has.

Docker is used through its command line. The `DockerTools` protocol is the part the tests replace.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

log = logging.getLogger(__name__)

TAG_REPOSITORY = "longhaul-swebench"
CONDA_ENV = "/opt/miniconda3/envs/testbed"
PLATFORM_VARIABLE = "LONGHAUL_SWEBENCH_PLATFORM"
# The harness publishes its images for x86_64 only. A machine of another architecture runs them
# under emulation, which is slow, so the eval VM is x86_64 (entry criteria of plan phase 9).
DEFAULT_PLATFORM = "linux/amd64"
PULL_TIMEOUT_S = 3600
BUILD_TIMEOUT_S = 1800
EXPORT_TIMEOUT_S = 900
TAIL_CHARS = 1_500

_PLATFORM = re.compile(r"[a-z0-9]+/[a-z0-9]+(?:/[a-z0-9]+)?")
_SHA = re.compile(r"[0-9a-f]{40}")


class ImageError(RuntimeError):
    """An image could not be pulled or built, or the repo could not be copied out of it."""


class InstanceImage(Protocol):
    """The three facts of an instance that this module reads."""

    @property
    def instance_id(self) -> str: ...

    @property
    def image(self) -> str: ...

    @property
    def base_commit(self) -> str: ...


class DockerTools(Protocol):
    def has_image(self, name: str) -> bool: ...

    def pull(self, name: str) -> None: ...

    def build(self, tag: str, dockerfile: str) -> None: ...

    def export_tree(self, image: str, base_commit: str) -> BinaryIO: ...


# ---------- the Dockerfile and the tag ----------
def derived_dockerfile(base_image: str) -> str:
    """The Dockerfile of the sandbox image that is made from an instance image."""
    return f"""\
FROM {base_image}
USER root

# Every command of the sandbox runs as this user.
RUN groupadd --gid 10001 agent \\
    && useradd --uid 10001 --gid 10001 --home-dir /home/agent --create-home --shell /bin/bash agent

# The task volume is mounted at /workspace. The editable install of the instance points at
# /testbed, so that path stays and leads to the repo. The history goes: the clone holds the
# commits after the base commit too, among them the fix, and the agent must not be able to read it.
# The worker starts a new repository here from the checkpoint repo of the task.
RUN mv /testbed /workspace \\
    && ln -s /workspace /testbed \\
    && rm -rf /workspace/.git \\
    && chown -R agent:agent /workspace

# The sandbox puts /workspace/.venv/bin first on PATH. Here that is the testbed environment.
RUN ln -s {CONDA_ENV} /workspace/.venv

# The supervisor of each command runs on this path. The base Python of conda is new enough.
RUN [ -e /usr/local/bin/python3 ] || ln -s /opt/miniconda3/bin/python3 /usr/local/bin/python3

# The search tool runs ripgrep.
RUN command -v rg >/dev/null \\
    || (apt-get update \\
        && apt-get install -y --no-install-recommends ripgrep \\
        && rm -rf /var/lib/apt/lists/*)

# The workspace and the checkpoint repo are owned by other users than git expects.
RUN git config --system user.name longhaul \\
    && git config --system user.email longhaul@localhost \\
    && git config --system --add safe.directory '*'

USER agent
WORKDIR /workspace
# The instance image starts a shell, which ends at once without a terminal. The sandbox idles.
ENTRYPOINT []
CMD ["sleep", "infinity"]
"""


def derived_tag(instance_id: str, base_image: str) -> str:
    """longhaul-swebench:<instance id>-<hash of the Dockerfile>.

    The hash is in the tag, so an image that was built from an older Dockerfile is not reused.
    """
    digest = hashlib.sha256(derived_dockerfile(base_image).encode("utf-8")).hexdigest()[:8]
    return f"{TAG_REPOSITORY}:{instance_id.lower()}-{digest}"


# ---------- the copy of the repo ----------
def export_script() -> str:
    """The shell script that runs in the instance image and writes the repo as a tar stream.

    The files are the ones of the working tree that git does not ignore: the tracked ones as the
    image has them, and the ones its install made that git neither tracks nor ignores. The agent
    starts from exactly the tree the harness runs the tests in. A tree that differs from the
    first commit of the checkpoint repo is "moved" for the workspace code, and its next restore
    deletes every file git ignores, which in an image with built extensions is the build.

    The base commit must be in the image, which tells a wrong image from a right one. The
    container is removed when the script ends.
    """
    return (
        "set -e\n"
        "cd /testbed\n"
        'git cat-file -e "$BASE_COMMIT^{commit}"\n'
        "git ls-files -z --cached --others --exclude-standard"
        " | tar --null --ignore-failed-read -T - -cf -\n"
    )


def extract_tree(stream: BinaryIO, folder: Path) -> int:
    """Write the regular files of a tar stream below `folder` and return how many there were.

    Links and special files are left out, as agent/workspace.py leaves them out of a local folder:
    the sandbox's file API cannot create them. A name that would leave the folder is an error.
    """
    count = 0
    try:
        with tarfile.open(fileobj=stream, mode="r|*") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                target = _inside(folder, member.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    continue
                with target.open("wb") as out:
                    while chunk := source.read(1 << 20):
                        out.write(chunk)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
                count += 1
    except tarfile.ReadError as exc:
        raise ImageError(f"the export of the repo holds no files: {exc}") from exc
    if count == 0:
        raise ImageError("the export of the repo holds no files")
    return count


def _inside(folder: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ImageError(f"the export names {name!r}, which is outside the folder")
    return folder.joinpath(*path.parts)


# ---------- docker ----------
def default_platform() -> str:
    """linux/amd64, or LONGHAUL_SWEBENCH_PLATFORM when it is set (for a stand-in image)."""
    value = os.environ.get(PLATFORM_VARIABLE)
    if value is None:
        return DEFAULT_PLATFORM
    if not _PLATFORM.fullmatch(value):
        raise ImageError(f"{PLATFORM_VARIABLE} must look like linux/amd64, not {value!r}")
    return value


class DockerCli:
    """DockerTools on the `docker` command. Needs a running daemon and the docker client."""

    def __init__(self, platform: str | None = None) -> None:
        self._platform = platform if platform is not None else default_platform()

    def _run(
        self, args: list[str], *, timeout_s: int, stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                ["docker", *args],
                input=stdin,
                capture_output=True,
                timeout=timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ImageError("the docker command is not installed or not on the PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ImageError(f"`docker {args[0]}` took longer than {timeout_s} s") from exc

    @staticmethod
    def _failure(what: str, done: subprocess.CompletedProcess[bytes]) -> ImageError:
        text = done.stderr.decode("utf-8", errors="replace").strip()
        return ImageError(f"{what} failed (exit {done.returncode}): {text[-TAIL_CHARS:]}")

    def has_image(self, name: str) -> bool:
        return self._run(["image", "inspect", name], timeout_s=60).returncode == 0

    def pull(self, name: str) -> None:
        log.info("pulling %s for %s", name, self._platform)
        done = self._run(["pull", "--platform", self._platform, name], timeout_s=PULL_TIMEOUT_S)
        if done.returncode != 0:
            raise self._failure(f"pulling {name}", done)

    def build(self, tag: str, dockerfile: str) -> None:
        log.info("building %s", tag)
        # The Dockerfile comes on stdin and there is no build context: it copies nothing.
        done = self._run(
            ["build", "--platform", self._platform, "--tag", tag, "-"],
            timeout_s=BUILD_TIMEOUT_S,
            stdin=dockerfile.encode("utf-8"),
        )
        if done.returncode != 0:
            raise self._failure(f"building {tag}", done)

    def _in_image(self, image: str, base_commit: str, script: str, what: str) -> bytes:
        """Run `script` in a container of `image` that is removed afterwards. Returns stdout."""
        if not _SHA.fullmatch(base_commit):
            raise ImageError(f"the base commit must be a full lowercase sha, not {base_commit!r}")
        done = self._run(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--platform",
                self._platform,
                "--user",
                "root",
                "--entrypoint",
                "sh",
                "--env",
                f"BASE_COMMIT={base_commit}",
                image,
                "-c",
                script,
            ],
            timeout_s=EXPORT_TIMEOUT_S,
        )
        if done.returncode != 0:
            raise self._failure(f"{what} {image}", done)
        return done.stdout

    def export_tree(self, image: str, base_commit: str) -> BinaryIO:
        out = self._in_image(image, base_commit, export_script(), "copying the repo out of")
        return io.BytesIO(out)


class DockerImages:
    """Makes the sandbox image of an instance and copies its repo to a folder."""

    def __init__(self, docker: DockerTools | None = None) -> None:
        self._docker = docker if docker is not None else DockerCli()

    def prepare(self, instance: InstanceImage, folder: Path) -> str:
        """Pull and build what is missing, write the repo to `folder`, return the image tag."""
        tag = derived_tag(instance.instance_id, instance.image)
        if not self._docker.has_image(instance.image):
            self._docker.pull(instance.image)
        if not self._docker.has_image(tag):
            self._docker.build(tag, derived_dockerfile(instance.image))
        stream = self._docker.export_tree(instance.image, instance.base_commit)
        extract_tree(stream, folder)
        return tag
