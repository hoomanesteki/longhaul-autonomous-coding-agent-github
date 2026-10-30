"""Docker-backed sandboxes. All repo code runs here, never on the worker."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import io
import json
import posixpath
import tarfile
import time
import uuid
from dataclasses import dataclass
from typing import Any

import docker
import requests
from docker.errors import APIError, NotFound

from longhaul.contracts import ExecResult, Sandbox, SandboxLimits
from longhaul.sandbox.common import WORKDIR, mount_for, resolve_path

AGENT_USER = "agent"
AGENT_UID = 10001
OUTPUT_CAP = 1_000_000
CONTAINER_PREFIX = "lh-sbx-"
TASK_LABEL = "longhaul.task_id"

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


def base_env() -> dict[str, str]:
    return dict(ENV_ALLOWLIST)


def merge_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Caller-supplied keys go over the allowlist for one exec only."""
    env = base_env()
    if extra:
        env.update(extra)
    return env


def cap_output(data: bytes | None, limit: int = OUTPUT_CAP) -> str:
    if not data:
        return ""
    text = data[:limit].decode("utf-8", errors="replace")
    if len(data) > limit:
        text += "\n[output truncated]"
    return text


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
        # `timeout --signal=KILL` kills bash and its children when time is up.
        # Plain `bash -c` is used: `-l` would source /etc/profile, which resets PATH.
        # Both programs are called by absolute path. The agent can write to the
        # venv directory that leads PATH, and a file named `timeout` or `bash`
        # there must not be able to switch the time limit off.
        argv = ["/usr/bin/timeout", "--signal=KILL", str(timeout_s), "/bin/bash", "-c", cmd]

        def run() -> tuple[int, Any]:
            res = self._container().exec_run(
                argv,
                demux=True,
                workdir=WORKDIR,
                user=AGENT_USER,
                environment=merge_env(env),
            )
            return int(res.exit_code), res.output

        start = time.monotonic()
        code, output = await asyncio.to_thread(run)
        elapsed = time.monotonic() - start
        out, err = output if output else (None, None)
        return ExecResult(
            exit_code=code,
            stdout=cap_output(out),
            stderr=cap_output(err),
            timed_out=code == 137 and elapsed >= timeout_s,
            duration_ms=int(elapsed * 1000),
        )

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
        self, task_id: str, image: str, limits: SandboxLimits, network: bool
    ) -> Sandbox:
        def run() -> str:
            # Always created on bridge: Docker cannot move a `none` container onto
            # a network later. When network=False we disconnect straight away.
            container = self._client.containers.run(
                image,
                name=container_name(task_id),
                detach=True,
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
                volumes={volume_name(task_id): {"bind": WORKDIR, "mode": "rw"}},
                network_mode="bridge",
                environment=base_env(),
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
