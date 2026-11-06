"""A Temporal and a Postgres of the suite's own, started and removed by the suite (LH-904).

The full suite restarts the Temporal server and Postgres on purpose. The pair that `make up`
starts serves every other build on the machine, so it must never be restarted. The suite starts a
second compose project from the same `docker-compose.yml`, with other host ports, and with the
volumes that compose gives every project by its name. The images, the command of the Temporal dev
server (with `--db-filename` on a volume, so a restart keeps the workflows) and the health checks
are the ones of the main file and are not copied.

Every container, volume and network the suite touches is found by the label of its own project.
`check_project` refuses any name without the prefix, which rules out the default project
(`longhaul`), so no call here can reach the shared stack.

The suite removes what it started object by object, found by the label of its project. It does not
run `docker compose down`, so a wrong project name could not take the shared stack down with it.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass

import docker
import psycopg
from docker.errors import DockerException, NotFound
from docker.models.containers import Container

from eval import custom_tasks
from eval.chaos import history

ROOT = custom_tasks.ROOT
COMPOSE_FILE = ROOT / "docker-compose.yml"
PROJECT_PREFIX = "longhaul-chaos-"
SERVICES = ("postgres", "temporal")
_PROJECT = re.compile(re.escape(PROJECT_PREFIX) + r"[a-z0-9]{4,32}")

# The credentials of the compose file. The data volume of a new project is made with them.
POSTGRES_USER = "longhaul"
POSTGRES_PASSWORD = "longhaul"
POSTGRES_DB = "longhaul"

START_TIMEOUT_S = 300
# How long docker waits for a container to stop before it kills it (compose restart uses 10).
STOP_TIMEOUT_S = 10
READY_TIMEOUT_S = 120


@dataclass(frozen=True)
class Ports:
    """The host ports of the private stack. Compose reads them from the LH_* variables."""

    postgres: int
    temporal: int
    temporal_ui: int


def new_project() -> str:
    return PROJECT_PREFIX + uuid.uuid4().hex[:8]


def check_project(name: str) -> str:
    """The name, or ValueError when it is not a project of this suite."""
    if not _PROJECT.fullmatch(name):
        raise ValueError(
            f"{name!r} is not a project of the chaos suite: its name must start with "
            f"{PROJECT_PREFIX!r} and be lower case letters and digits after it"
        )
    return name


def check_service(name: str) -> str:
    if name not in SERVICES:
        raise ValueError(f"the suite can restart {', '.join(SERVICES)}, not {name!r}")
    return name


def free_ports(count: int) -> list[int]:
    """`count` distinct ports that nothing listens on now, all on 127.0.0.1."""
    held: list[socket.socket] = []
    try:
        for _ in range(count):
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            held.append(probe)
        return [probe.getsockname()[1] for probe in held]
    finally:
        for probe in held:
            probe.close()


def compose_env(ports: Ports) -> dict[str, str]:
    return {
        "LH_POSTGRES_PORT": str(ports.postgres),
        "LH_TEMPORAL_PORT": str(ports.temporal),
        "LH_TEMPORAL_UI_PORT": str(ports.temporal_ui),
    }


def up_command(project: str) -> list[str]:
    return [
        "docker",
        "compose",
        "-p",
        check_project(project),
        "-f",
        str(COMPOSE_FILE),
        "up",
        "-d",
        "--wait",
        *SERVICES,
    ]


def label_filters(project: str, service: str | None = None) -> dict[str, str | list[str] | bool]:
    """Docker filters for the objects of a project, or of one of its services."""
    labels = [f"com.docker.compose.project={check_project(project)}"]
    if service is not None:
        labels.append(f"com.docker.compose.service={check_service(service)}")
    return {"label": labels}


def database_url(ports: Ports, database: str = POSTGRES_DB) -> str:
    credentials = f"{POSTGRES_USER}:{POSTGRES_PASSWORD}"
    return f"postgresql://{credentials}@127.0.0.1:{ports.postgres}/{database}"


def temporal_address(ports: Ports) -> str:
    return f"127.0.0.1:{ports.temporal}"


class PrivateServices:
    """The running private stack. Make one with `start`, and always call `remove`."""

    def __init__(self, project: str, ports: Ports) -> None:
        self.project = check_project(project)
        self.ports = ports

    @classmethod
    def start(cls) -> PrivateServices:
        """Start Postgres and Temporal and wait until both are healthy."""
        ports = Ports(*free_ports(3))
        services = cls(new_project(), ports)
        env = {**os.environ, **compose_env(ports)}
        try:
            subprocess.run(
                up_command(services.project),
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
                timeout=START_TIMEOUT_S,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = exc.stderr if isinstance(exc, subprocess.CalledProcessError) else ""
            services.remove()
            raise RuntimeError(f"could not start {services.project}: {exc}\n{detail}") from exc
        try:
            services.wait_for_postgres()
        except BaseException:
            services.remove()
            raise
        return services

    def wait_for_postgres(self) -> None:
        """Wait until Postgres accepts a connection on its port.

        The health check of the compose file asks over the socket. The image first starts a
        temporary server that listens on the socket only, and the check passes while it runs.
        The real server comes up a moment later, so the port is tried before anything uses it.
        """
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            try:
                psycopg.connect(self.database, connect_timeout=2).close()
                return
            except psycopg.OperationalError as exc:
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"Postgres of {self.project} is not reachable: {exc}"
                    ) from exc
                time.sleep(0.5)

    @property
    def database(self) -> str:
        return database_url(self.ports)

    @property
    def temporal(self) -> str:
        return temporal_address(self.ports)

    def restart(self, service: str) -> bool:
        """Restart a container of the project, and return when it is running again."""
        client = docker.from_env()
        try:
            found = client.containers.list(filters=label_filters(self.project, service))
            for container in found:
                container.restart(timeout=STOP_TIMEOUT_S)
            return bool(found)
        finally:
            client.close()

    def clock_offset_s(self) -> float:
        """Seconds the clock of the Docker VM is ahead of this machine's."""
        client = docker.from_env()
        try:
            (container,) = client.containers.list(filters=label_filters(self.project, "postgres"))
            before = time.time()
            code, output = container.exec_run(["date", "-u", "+%s.%N"])
            after = time.time()
            reading = output.decode() if isinstance(output, bytes) else ""
            if code != 0 or not reading.strip():
                raise RuntimeError(f"date failed in {container.name}: {reading!r}")
            return history.clock_offset_s(float(reading.strip()), before, after)
        finally:
            client.close()

    def remove(self) -> None:
        """Remove the containers, volumes and networks of this project and nothing else."""
        client = docker.from_env()
        try:
            filters = label_filters(self.project)
            for container in client.containers.list(all=True, filters=filters):
                _forget(container)
            for volume in client.volumes.list(filters=filters):
                with contextlib.suppress(DockerException):
                    volume.remove(force=True)
            for network in client.networks.list(filters=filters):
                with contextlib.suppress(DockerException):
                    network.remove()
        finally:
            client.close()


def _forget(container: Container) -> None:
    with contextlib.suppress(NotFound):
        container.remove(force=True, v=True)


def kill_sandbox(task_id: str) -> bool:
    """`docker kill` the sandbox container of a task. True when one was running."""
    client = docker.from_env()
    try:
        label: dict[str, str | list[str] | bool] = {"label": f"longhaul.task_id={task_id}"}
        running = client.containers.list(filters=label)
        for container in running:
            container.kill()
        return bool(running)
    finally:
        client.close()
