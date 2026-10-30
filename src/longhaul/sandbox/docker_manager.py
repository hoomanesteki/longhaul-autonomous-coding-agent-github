"""Docker-backed sandboxes. All repo code runs here, never on the worker."""

from __future__ import annotations

import asyncio
import contextlib
import io
import posixpath
import tarfile
import time
import uuid
from typing import Any

import docker
from docker.errors import NotFound

from longhaul.contracts import ExecResult, Sandbox, SandboxLimits

WORKDIR = "/workspace"
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


def resolve_path(path: str) -> str:
    joined = path if path.startswith("/") else posixpath.join(WORKDIR, path)
    return posixpath.normpath(joined)


WRITABLE_MOUNTS = (WORKDIR, "/tmp", "/home/agent")  # noqa: S108


def mount_for(path: str) -> str:
    """Writable mount that holds `path`. Docker refuses put_archive elsewhere."""
    for mount in WRITABLE_MOUNTS:
        if path == mount or path.startswith(mount + "/"):
            return mount
    raise PermissionError(f"path is not on a writable mount: {path}")


def build_tar(path: str, data: bytes) -> bytes:
    """Tar holding one file, owned by the agent user, with parent dirs included.

    `path` is relative to the extraction directory.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        parts = path.strip("/").split("/")
        for i in range(1, len(parts)):
            d = tarfile.TarInfo("/".join(parts[:i]))
            d.type = tarfile.DIRTYPE
            d.mode = 0o755
            d.uid = d.gid = AGENT_UID
            tar.addfile(d)
        info = tarfile.TarInfo(path.strip("/"))
        info.size = len(data)
        info.mode = 0o644
        info.uid = info.gid = AGENT_UID
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


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

    async def read_file(self, path: str) -> bytes:
        full = resolve_path(path)

        def run() -> bytes:
            stream, _ = self._container().get_archive(full)
            buf = io.BytesIO(b"".join(stream))
            with tarfile.open(fileobj=buf) as tar:
                member = tar.next()
                if member is None or not member.isfile():
                    raise IsADirectoryError(full)
                f = tar.extractfile(member)
                assert f is not None
                return f.read()

        return await asyncio.to_thread(run)

    async def write_file(self, path: str, data: bytes) -> None:
        full = resolve_path(path)
        # The root fs is read-only, so extract inside the mount that holds the
        # path. Parent dirs come from the archive and are owned by the agent user.
        mount = mount_for(full)
        archive = build_tar(posixpath.relpath(full, mount), data)
        await asyncio.to_thread(self._container().put_archive, mount, archive)

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
