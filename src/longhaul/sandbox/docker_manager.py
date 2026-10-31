"""Docker-backed sandboxes. All repo code runs here, never on the worker."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import io
import json
import os
import posixpath
import tarfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import docker
import requests
from docker.errors import APIError, NotFound

from longhaul.contracts import ExecResult, Sandbox, SandboxLimits
from longhaul.sandbox.common import (
    WORKDIR,
    WRITABLE_MOUNTS,
    BoundedOutput,
    check_timeout,
    mount_for,
    resolve_path,
)

AGENT_USER = "agent"
AGENT_UID = 10001
CONTAINER_PREFIX = "lh-sbx-"
TASK_LABEL = "longhaul.task_id"

# How long to wait for the daemon to record an exit status after the output ends.
EXIT_POLLS = 100
EXIT_POLL_S = 0.05

# The root filesystem is read-only, so packages for the repo's tests go into a
# virtualenv inside the workspace volume. Its bin directory leads PATH so pytest
# and friends are found without activating the venv. When the venv does not
# exist the entry is simply skipped.
VENV_BIN = f"{WORKDIR}/.venv/bin"
# The image's own PATH.
IMAGE_PATH = "/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# The only variables a sandbox ever sees (invariant I3). Values are fixed here
# and never read from os.environ, so worker secrets cannot leak in by accident.
ENV_ALLOWLIST: dict[str, str] = {
    "PATH": f"{VENV_BIN}:{IMAGE_PATH}",
    "HOME": "/home/agent",
    "LANG": "C.UTF-8",
    "PYTHONUNBUFFERED": "1",
    # An edit keeps the byte size, and Python trusts a .pyc whose source mtime and
    # size match. Without bytecode there is nothing stale to run after an edit.
    "PYTHONDONTWRITEBYTECODE": "1",
}


def container_name(task_id: str, suffix: str | None = None) -> str:
    return f"{CONTAINER_PREFIX}{task_id}-{suffix or uuid.uuid4().hex[:8]}"


def volume_name(task_id: str) -> str:
    return f"lh-ws-{task_id}"


def _overlaps(a: str, b: str) -> bool:
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def bind_mounts(mounts: Mapping[str, str] | None) -> dict[str, dict[str, str]]:
    """Docker `volumes` entries for host folders that go into the sandbox, read-write.

    `mounts` maps a host folder to a container path. Everything is checked before a container
    exists: Docker would create a missing host folder as root, and a bad target would only
    fail halfway through the start.
    """
    binds: dict[str, dict[str, str]] = {}
    targets: list[str] = []
    for host, target in (mounts or {}).items():
        real = _real_host_folder(host)
        _check_target(target, targets)
        if real in binds:
            raise ValueError(f"mount source {real} is mounted twice")
        binds[real] = {"bind": target, "mode": "rw"}
        targets.append(target)
    return binds


def _real_host_folder(host: str) -> str:
    if not os.path.isabs(host):
        raise ValueError(f"mount source {host!r} must be an absolute path")
    # Docker Desktop shares real paths, and /tmp and /var are links on a Mac.
    real = os.path.realpath(host)
    if real == "/":
        raise ValueError("the host root cannot be mounted into a sandbox")
    if not os.path.isdir(real):
        raise ValueError(f"mount source {host!r} is not a directory")
    return real


def _check_target(target: str, taken: list[str]) -> None:
    if not posixpath.isabs(target) or posixpath.normpath(target) != target or target == "/":
        raise ValueError(f"container path {target!r} must be absolute and normalized, not /")
    # The manager mounts these itself: the workspace volume and the two tmpfs folders.
    if any(_overlaps(target, mount) for mount in WRITABLE_MOUNTS):
        raise ValueError(f"container path {target!r} would cover a writable folder of the sandbox")
    if any(_overlaps(target, other) for other in taken):
        raise ValueError(f"container path {target!r} would overlap another mount")


def base_env() -> dict[str, str]:
    return dict(ENV_ALLOWLIST)


def merge_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Caller-supplied keys go over the allowlist for one exec only."""
    env = base_env()
    if extra:
        env.update(extra)
    return env


# The agent can write to the venv directory that leads PATH, so a file named
# `python3`, `timeout` or `bash` there must not be able to take the place of the
# real program, least of all the one that enforces the time limit.
PYTHON = "/usr/local/bin/python3"
TIMEOUT = "/usr/bin/timeout"
BASH = "/bin/bash"

# What runs inside the container around every command: see supervisor.py. It is sent
# as text with each call, so it always matches this code and needs nothing installed
# in the image except python3 at the path above, which the official python images
# have. An image without it fails every exec loudly instead of skipping the cleanup.
SUPERVISOR_SOURCE = Path(__file__).with_name("supervisor.py").read_text(encoding="utf-8")


def exec_argv(cmd: str, timeout_s: float) -> list[str]:
    """The command line for one `Sandbox.exec` call.

    `timeout --signal=KILL` ends bash and the processes in its group when time is
    up. The supervisor around it also kills what escaped the group (setsid, daemons)
    and what a command left in the background. Plain `bash -c` is used: `-l` would
    source /etc/profile, which resets PATH. `-I` keeps the working directory (the
    agent's files) and PYTHON* variables out of the supervisor's own Python, and `-S`
    skips the site machinery it does not need.
    """
    # GNU timeout reads 0 as "no limit", which would switch the F7 guarantee off.
    check_timeout(timeout_s)
    supervisor = [PYTHON, "-I", "-S", "-c", SUPERVISOR_SOURCE]
    limited = [TIMEOUT, "--signal=KILL", str(timeout_s), BASH, "-c", cmd]
    return supervisor + limited


def build_tar(path: str, data: bytes, *, mode: int = 0o644) -> bytes:
    """Tar holding one file, owned by the agent user, with parent dirs included.

    `path` is relative to the extraction directory, and every directory in it is
    created: the caller passes only the directories that do not exist yet,
    because an entry for an existing one would reset its mode and mtime.
    Every entry gets the current time. An mtime of 0 would leave Python trusting
    an old .pyc after a same-size edit.
    """
    now = time.time()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            d = tarfile.TarInfo("/".join(parts[:i]))
            d.type = tarfile.DIRTYPE
            d.mode = 0o755
            d.uid = d.gid = AGENT_UID
            d.mtime = now
            tar.addfile(d)
        info = tarfile.TarInfo(path.strip("/"))
        info.size = len(data)
        info.mode = mode
        info.uid = info.gid = AGENT_UID
        info.mtime = now
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# Docker reports the mode of a path as a Go os.FileMode: the permission bits plus
# one flag per file type.
_MODE_DIR = 1 << 31
_MODE_SYMLINK = 1 << 27
_MODE_SPECIAL = (1 << 26) | (1 << 25) | (1 << 24) | (1 << 21) | (1 << 19)  # device, pipe, socket...


@dataclass(frozen=True)
class PathStat:
    """What Docker's archive API says about one path, without following a final symlink."""

    mode: int
    link_target: str  # fully resolved by Docker, so a chain of links is already one hop

    @property
    def is_dir(self) -> bool:
        return bool(self.mode & _MODE_DIR)

    @property
    def is_symlink(self) -> bool:
        return bool(self.mode & _MODE_SYMLINK)

    @property
    def is_regular(self) -> bool:
        return not self.mode & (_MODE_DIR | _MODE_SYMLINK | _MODE_SPECIAL)

    @property
    def permissions(self) -> int:
        return self.mode & 0o777


def parse_stat(header: str) -> PathStat:
    """Read the X-Docker-Container-Path-Stat header: base64 of a JSON object."""
    raw = json.loads(base64.b64decode(header))
    return PathStat(mode=int(raw["mode"]), link_target=raw.get("linkTarget") or "")


# Linux gives up on a path after this many symlinks.
MAX_LINK_HOPS = 40


@dataclass(frozen=True)
class Resolved:
    """Where a path leads once its symlinks are followed, and what is there."""

    path: str
    found: PathStat | None  # the last name; None when it does not exist
    parent: str  # the deepest directory that exists on the way
    new_names: list[str]  # names still to be created below `parent`, the last one is the file

    @property
    def name(self) -> str:
        return posixpath.basename(self.path)


def _message(res: requests.Response) -> str:
    try:
        return str(res.json()["message"])
    except (ValueError, KeyError, TypeError):
        return res.text[:300]


class DockerSandbox:
    workdir = WORKDIR

    def __init__(self, client: docker.DockerClient, container_id: str) -> None:
        self._client = client
        self.id = container_id

    def _container(self) -> Any:
        return self._client.containers.get(self.id)

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        argv = exec_argv(cmd, timeout_s)  # also refuses a timeout that is not positive
        start = time.monotonic()
        code, out, err = await asyncio.to_thread(self._stream_exec, argv, merge_env(env))
        elapsed = time.monotonic() - start
        return ExecResult(
            exit_code=code,
            stdout=out.text(),
            stderr=err.text(),
            timed_out=code == 137 and elapsed >= timeout_s,
            duration_ms=int(elapsed * 1000),
        )

    def _stream_exec(
        self, argv: list[str], env: dict[str, str]
    ) -> tuple[int, BoundedOutput, BoundedOutput]:
        """Run `argv` and read its output as it arrives, keeping the head and tail.

        exec_run() returns only after it has collected everything. The worker holds
        the credentials and has no memory limit, so a command that printed a lot
        would grow it without bound. Here the stream is drained and trimmed as it goes.
        """
        api = self._client.api
        created = api.exec_create(self.id, argv, workdir=WORKDIR, user=AGENT_USER, environment=env)
        exec_id = str(created["Id"])
        out, err = BoundedOutput(), BoundedOutput()
        stream = api.exec_start(exec_id, stream=True, demux=True)
        try:
            for stdout, stderr in stream:
                if stdout:
                    out.feed(stdout)
                if stderr:
                    err.feed(stderr)
        finally:
            # The caller owns the connection of a stream. The peer may be gone already.
            with contextlib.suppress(OSError):
                stream.close()
        return self._exit_code(exec_id), out, err

    def _exit_code(self, exec_id: str) -> int:
        """The exit status once the daemon has recorded it.

        The output stream can end a moment before that, and the status reads as
        None until then.
        """
        api = self._client.api
        for _ in range(EXIT_POLLS):
            info = api.exec_inspect(exec_id)
            if not info["Running"] and info["ExitCode"] is not None:
                return int(info["ExitCode"])
            time.sleep(EXIT_POLL_S)
        raise RuntimeError(f"exec {exec_id} ended without an exit status")

    def _read(self, full: str) -> bytes:
        target = self._resolve(full)
        if target.found is None:
            raise FileNotFoundError(errno.ENOENT, "no such file or directory", target.path)
        if target.found.is_dir:
            raise IsADirectoryError(errno.EISDIR, "is a directory", target.path)
        if not target.found.is_regular:
            raise OSError(errno.EINVAL, "not a regular file", target.path)
        api = self._client.api
        try:
            stream, _ = api.get_archive(self.id, target.path)
            raw = b"".join(stream)
        except NotFound:
            api.inspect_container(self.id)  # raises NotFound when the container is gone
            raise FileNotFoundError(
                errno.ENOENT, "no such file or directory", target.path
            ) from None
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            member = tar.next()
            if member is None or not member.isfile():
                raise IsADirectoryError(errno.EISDIR, "is a directory", target.path)
            f = tar.extractfile(member)
            assert f is not None
            return f.read()

    async def read_file(self, path: str) -> bytes:
        full = resolve_path(path)
        mount_for(full)  # the same roots as write_file, so '..' cannot climb out
        return await asyncio.to_thread(self._read, full)

    # The archive API cannot do what open() does, so the rules are applied here:
    # put_archive replaces whatever sits at a path with the type in the tar, which
    # would turn a directory into a file or a file into a directory without an error.

    def _archive_url(self) -> str:
        api = self._client.api
        return f"{api.base_url}/v{api.api_version}/containers/{self.id}/archive"

    def _stat(self, path: str) -> PathStat | None:
        """Like lstat: what is at `path`, or None when nothing is."""
        api = self._client.api
        res = api.head(self._archive_url(), params={"path": path}, timeout=api.timeout)
        if res.status_code == 200:
            return parse_stat(res.headers["X-Docker-Container-Path-Stat"])
        if res.status_code == 404:
            # HEAD has no body, so a missing container looks like a missing path.
            api.inspect_container(self.id)  # raises NotFound when the container is gone
            return None
        raise APIError(f"cannot stat {path}", response=res)

    def _walk(self, path: str) -> Resolved | str:
        """Check `path` one name at a time from its mount down.

        Returns what was found, or the path to carry on with when a symlink was met.
        """
        mount = mount_for(path)
        if mount != WORKDIR:
            # Docker's archive API does not see tmpfs mounts: a file made there by a
            # command reads as "not found" and put_archive is refused. Say so.
            raise PermissionError(
                errno.EACCES, f"{mount} is a tmpfs mount, which the file API cannot reach", path
            )
        names = [name for name in path[len(mount) :].split("/") if name]
        if not names:
            root = self._stat(mount)
            if root is None:
                raise FileNotFoundError(errno.ENOENT, "no such directory", mount)
            return Resolved(mount, root, mount, [])
        parent = mount
        for index, name in enumerate(names):
            below = names[index + 1 :]
            child = posixpath.join(parent, name)
            found = self._stat(child)
            if found is None:
                # Nothing here, so nothing below it either.
                return Resolved(posixpath.join(child, *below), None, parent, [name, *below])
            if found.is_symlink:
                # Docker has already followed the whole chain. The target may lie on
                # another mount or outside every mount, which mount_for() refuses.
                return posixpath.join(found.link_target, *below)
            if not below:
                return Resolved(child, found, parent, [])
            if not found.is_dir:
                raise NotADirectoryError(errno.ENOTDIR, "not a directory", child)
            parent = child
        raise AssertionError("unreachable: names is not empty")  # pragma: no cover

    def _resolve(self, path: str) -> Resolved:
        """Like realpath for the part of `path` that exists, with the same limits on links."""
        for _ in range(MAX_LINK_HOPS):
            step = self._walk(path)
            if isinstance(step, Resolved):
                return step
            path = step
        raise OSError(errno.ELOOP, "too many levels of symbolic links", path)

    def _extract(self, directory: str, archive: bytes) -> None:
        """Unpack `archive` into `directory`, refusing to swap a file for a directory."""
        api = self._client.api
        # noOverwriteDirNonDir is the daemon's own guard if the tree changes
        # between our stat and this call.
        params: dict[str, str | int] = {"path": directory, "noOverwriteDirNonDir": 1}
        res = api.put(self._archive_url(), params=params, data=archive, timeout=api.timeout)
        if res.status_code == 200:
            return
        message = _message(res)
        if res.status_code == 404 and message.startswith("No such container"):
            raise NotFound(message, response=res)
        if res.status_code == 404:
            raise FileNotFoundError(errno.ENOENT, "no such directory", directory)
        if "read-only" in message:
            raise PermissionError(errno.EACCES, f"{directory} is not on the workspace volume")
        if "cannot overwrite directory" in message:
            raise IsADirectoryError(errno.EISDIR, "is a directory", directory)
        if "not a directory" in message or "cannot overwrite non-directory" in message:
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", directory)
        raise APIError(message, response=res)

    def _write(self, full: str, data: bytes) -> None:
        target = self._resolve(full)
        if target.found is None:
            mode = 0o644
        elif target.found.is_dir:
            raise IsADirectoryError(errno.EISDIR, "is a directory", target.path)
        elif not target.found.is_regular:
            raise FileExistsError(errno.EEXIST, "not a regular file", target.path)
        else:
            # The file is replaced, not rewritten in place, so its mode is passed on.
            mode = target.found.permissions
        # Only directories that do not exist yet go into the tar. The daemon would
        # create them as root, so the tar lists them with the agent as owner.
        relative = "/".join(target.new_names) if target.found is None else target.name
        self._extract(target.parent, build_tar(relative, data, mode=mode))

    async def write_file(self, path: str, data: bytes) -> None:
        full = resolve_path(path)
        mount_for(full)
        await asyncio.to_thread(self._write, full, data)

    async def destroy(self) -> None:
        def run() -> None:
            with contextlib.suppress(NotFound):
                self._container().remove(force=True)

        await asyncio.to_thread(run)


class DockerSandboxManager:
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self._client = client or docker.from_env()

    async def create(
        self,
        task_id: str,
        image: str,
        limits: SandboxLimits,
        network: bool,
        mounts: Mapping[str, str] | None = None,
    ) -> Sandbox:
        """Start a sandbox. `mounts` maps host folders to container paths, read-write.

        It is not part of the SandboxManager protocol. The checkpoint store uses it to put a
        task's bare repo at /ckpt (ADR-0006); see bind_mounts for the rules.
        """
        binds = bind_mounts(mounts)  # before anything is created, so a bad mount leaves nothing

        def run() -> str:
            # Always created on bridge: Docker cannot move a `none` container onto
            # a network later. When network=False we disconnect straight away.
            container = self._client.containers.run(
                image,
                name=container_name(task_id),
                detach=True,
                # PID 1 is an init that reaps, not `sleep infinity`. A process whose parent
                # has gone is adopted by PID 1, and without a reaper it stays a zombie that
                # holds a pid until fork fails and nothing can run.
                init=True,
                user=AGENT_USER,
                labels={TASK_LABEL: task_id},
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                pids_limit=limits.pids,
                mem_limit=f"{limits.memory_mb}m",
                nano_cpus=int(limits.cpus * 1e9),
                read_only=True,
                # Root fs is read-only, so tools need writable scratch space. HOME
                # gets its own tmpfs because git and pip write config and caches there.
                tmpfs={
                    "/tmp": "size=512m",
                    "/home/agent": f"size=64m,uid={AGENT_UID},gid={AGENT_UID}",
                },
                volumes={volume_name(task_id): {"bind": WORKDIR, "mode": "rw"}, **binds},
                network_mode="bridge",
                environment=base_env(),
                # docker-py would add the proxies block of ~/.docker/config.json to the
                # environment, credentials included. The allowlist is all the sandbox gets.
                use_config_proxy=False,
            )
            if not network:
                self._client.networks.get("bridge").disconnect(container)
            return str(container.id)

        return DockerSandbox(self._client, await asyncio.to_thread(run))

    async def get(self, sandbox_id: str) -> Sandbox | None:
        def run() -> bool:
            try:
                c = self._client.containers.get(sandbox_id)
            except NotFound:
                return False
            return bool(c.status == "running")

        if await asyncio.to_thread(run):
            return DockerSandbox(self._client, sandbox_id)
        return None

    async def set_network(self, sandbox_id: str, enabled: bool) -> None:
        def run() -> None:
            container = self._client.containers.get(sandbox_id)
            attached = "bridge" in container.attrs["NetworkSettings"]["Networks"]
            bridge = self._client.networks.get("bridge")
            if enabled and not attached:
                bridge.connect(container)
            elif not enabled and attached:
                bridge.disconnect(container)

        await asyncio.to_thread(run)

    async def cleanup_task(self, task_id: str) -> None:
        def run() -> None:
            label = f"{TASK_LABEL}={task_id}"
            for c in self._client.containers.list(all=True, filters={"label": label}):
                c.remove(force=True)
            with contextlib.suppress(NotFound):
                self._client.volumes.get(volume_name(task_id)).remove(force=True)

        await asyncio.to_thread(run)
