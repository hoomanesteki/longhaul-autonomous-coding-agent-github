"""The durable stack end to end: `longhaul worker` and `longhaul run` as separate processes.

The worker is started the way an operator starts it, as the `longhaul` script of the project's
virtualenv, against the compose Temporal, Postgres and MinIO and the local Docker daemon. Run
`make up` and `make sandbox-image` first. The workspace setup builds a venv and installs pytest
and calc from PyPI, so the machine needs outbound network.

Nothing here touches what other work on the machine uses. Each test gets its own task queue, so
no other worker picks up its tasks, its own scratch database (migrated to head, then dropped),
its own checkpoint folder, and task ids with a random suffix. At the end it removes what it
made: the containers and volumes of its tasks, their blobs, and any workflow still open.

The script reaches the worker through the blob store, because the worker is started without a
script: `longhaul run --provider scripted --script FILE` uploads it with the task.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.util
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
import docker
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from temporalio.client import WorkflowExecutionStatus, WorkflowHandle

from longhaul.contracts import Phase
from longhaul.durable.client import connect, get_task, read_status
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.e2e, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
SCRIPT = ROOT / "tests" / "fixtures" / "scripts" / "calc_fix.yaml"
CONFIG = ROOT / "config" / "longhaul.example.yaml"
IMAGE = "longhaul-sandbox:py312"
# `uv run` and `uv sync` put the script next to the interpreter.
LONGHAUL = Path(sys.executable).with_name("longhaul")
DIFF_MARKER = "final diff against base:"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_e2e_"
RUN_WAIT_S = 900
HEADER = re.compile(r"^task (lh-local-calc-[0-9a-f]{8}):", re.MULTILINE)

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git
isolated_git_env = _module.isolated_git_env


# ---------- the services ----------
@functools.cache
def service_settings() -> dict[str, str]:
    """Where the compose services are. A .env that remaps a port applies here as it does to make.

    Read once, before the database fixture points LONGHAUL_DATABASE_URL somewhere else.
    """
    settings = EnvSettings()
    database = settings.database_url.get_secret_value() if settings.database_url else LOCAL_DSN
    access, secret = settings.blob_access_key, settings.blob_secret_key
    return {
        "database": database,
        "temporal": settings.temporal_address,
        "namespace": settings.temporal_namespace,
        "blob_endpoint": settings.blob_endpoint or "http://localhost:9000",
        "blob_bucket": settings.blob_bucket or "longhaul",
        "blob_access_key": access.get_secret_value() if access else "longhaul",
        "blob_secret_key": secret.get_secret_value() if secret else "longhaul-secret",
    }


def require_image() -> None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, check=False
    )
    if inspect.returncode != 0:
        pytest.fail(f"image {IMAGE} is missing; run make sandbox-image first")


def require_script() -> None:
    if not LONGHAUL.exists():
        pytest.fail(f"{LONGHAUL} is missing; run uv sync, which installs the [project.scripts]")


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh database at the head revision. Alembic must not reach the application database."""
    server = make_url(service_settings()["database"])
    admin = str(server.set(database="postgres").render_as_string(hide_password=False))
    name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
    url = str(server.set(database=name).render_as_string(hide_password=False))
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", "postgresql://nobody:nopass@127.0.0.1:1/never")
    get_settings.cache_clear()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield url
    finally:
        get_settings.cache_clear()
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


# ---------- a private stack for one test ----------
@dataclass
class Durable:
    """One test's worker processes, run commands and the task ids it made, with their clean-up."""

    tmp_path: Path
    database: str
    queue: str = field(default_factory=lambda: f"lh-e2e-{uuid.uuid4().hex[:8]}")
    processes: list[subprocess.Popen[str]] = field(default_factory=list)
    logs: dict[int, Path] = field(default_factory=dict)  # by pid
    task_ids: list[str] = field(default_factory=list)

    @property
    def settings(self) -> dict[str, str]:
        return service_settings()

    def environment(self) -> dict[str, str]:
        """The machine's environment without any longhaul setting or model key, plus this stack.

        CI has no keys, and a scripted run must not need one. The worker gets no script and no
        provider setting of its own, so the only way a script can reach it is the task.
        """
        drop = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"}
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("LONGHAUL_") and k not in drop
        }
        # The console script finds the package through the editable install's .pth file, which
        # Python on macOS skips when the file carries the hidden flag. Name src explicitly.
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")])
        )
        s = self.settings
        env.update(
            LONGHAUL_DATABASE_URL=self.database,
            LONGHAUL_TEMPORAL_ADDRESS=s["temporal"],
            LONGHAUL_TEMPORAL_NAMESPACE=s["namespace"],
            LONGHAUL_TASK_QUEUE=self.queue,
            LONGHAUL_BLOB_ENDPOINT=s["blob_endpoint"],
            LONGHAUL_BLOB_BUCKET=s["blob_bucket"],
            LONGHAUL_BLOB_ACCESS_KEY=s["blob_access_key"],
            LONGHAUL_BLOB_SECRET_KEY=s["blob_secret_key"],
            LONGHAUL_CKPT_DIR=str(self.tmp_path / "ckpt"),
            LONGHAUL_CONFIG=str(CONFIG),
        )
        return env

    def spawn(self, *args: str) -> subprocess.Popen[str]:
        """Start `longhaul <args>` in its own session, with stdout and stderr in one file."""
        require_script()
        log = self.tmp_path / f"process-{len(self.processes)}.log"
        with log.open("w") as out:
            proc = subprocess.Popen(
                [str(LONGHAUL), *args],
                cwd=self.tmp_path,  # not the repo root, to show that the config is found anyway
                env=self.environment(),
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        self.processes.append(proc)
        self.logs[proc.pid] = log
        return proc

    def start_worker(self) -> subprocess.Popen[str]:
        return self.spawn("worker")

    def start_run(self) -> subprocess.Popen[str]:
        return self.spawn(
            "run",
            "--path",
            str(CALC),
            "--issue-file",
            str(CALC / "ISSUE.md"),
            "--provider",
            "scripted",
            "--script",
            str(SCRIPT),
        )

    def output_of(self, proc: subprocess.Popen[str]) -> str:
        return self.logs[proc.pid].read_text(encoding="utf-8")

    def wait_for_text(self, proc: subprocess.Popen[str], text: str, seconds: float = 120) -> None:
        """Wait until the process has printed `text`, and fail early if it has ended."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if text in self.output_of(proc):
                return
            assert proc.poll() is None, f"the process ended first:\n{self.output_of(proc)}"
            time.sleep(0.2)
        pytest.fail(f"{text!r} did not appear in {seconds:.0f} s:\n{self.output_of(proc)}")

    def task_id_of(self, proc: subprocess.Popen[str]) -> str:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            found = HEADER.search(self.output_of(proc))
            if found:
                self.task_ids.append(found.group(1))
                return found.group(1)
            assert proc.poll() is None, f"the run ended first:\n{self.output_of(proc)}"
            time.sleep(0.2)
        pytest.fail(f"the run printed no task id:\n{self.output_of(proc)}")

    def close(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        task_ids = {*self.task_ids, *self.task_ids_in_logs()}
        for task_id in task_ids:
            self.remove_sandbox(task_id)
            self.remove_blobs(task_id)
        self.terminate_workflows(task_ids)

    def task_ids_in_logs(self) -> set[str]:
        return {
            found
            for log in self.tmp_path.glob("process-*.log")
            for found in HEADER.findall(log.read_text(encoding="utf-8", errors="replace"))
        }

    @staticmethod
    def remove_sandbox(task_id: str) -> None:
        client = docker.from_env()
        label = {"label": f"longhaul.task_id={task_id}"}
        for container in client.containers.list(all=True, filters=label):
            container.remove(force=True)
        for volume in client.volumes.list(filters={"name": f"lh-ws-{task_id}"}):
            volume.remove(force=True)

    def remove_blobs(self, task_id: str) -> None:
        s = self.settings
        s3 = boto3.client(
            "s3",
            endpoint_url=s["blob_endpoint"],
            aws_access_key_id=s["blob_access_key"],
            aws_secret_access_key=s["blob_secret_key"],
            region_name="us-east-1",
        )
        listing = s3.list_objects_v2(Bucket=s["blob_bucket"], Prefix=f"tasks/{task_id}/")
        keys = [{"Key": item["Key"]} for item in listing.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=s["blob_bucket"], Delete={"Objects": keys})

    def terminate_workflows(self, task_ids: set[str]) -> None:
        async def terminate() -> None:
            client = await connect(self.settings["temporal"], namespace=self.settings["namespace"])
            for task_id in task_ids:
                handle = get_task(client, task_id)
                if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                    await handle.terminate(reason="the test ended")

        # Best effort: the server may be the thing that failed.
        with contextlib.suppress(Exception):
            asyncio.run(terminate())


@pytest.fixture
def durable(tmp_path: Path, database: str) -> Iterator[Durable]:
    stack = Durable(tmp_path, database)
    try:
        yield stack
    finally:
        stack.close()


# ---------- checking a result ----------
def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
    }


def git(repo: Path, *args: str) -> None:
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=isolated_git_env(),
        check=False,
    )
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"


def files_of(repo: Path) -> dict[str, bytes]:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in sorted(repo.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }


def leftovers(task_id: str) -> tuple[list[str], list[str]]:
    client = docker.from_env()
    containers = client.containers.list(all=True, filters={"label": f"longhaul.task_id={task_id}"})
    volumes = client.volumes.list(filters={"name": f"lh-ws-{task_id}"})
    return [str(c.name) for c in containers], [str(v.name) for v in volumes]


def assert_the_reference_fix(output: str, tmp_path: Path) -> None:
    """The printed diff is the fix and nothing else, and it gives the same files as the reference.

    The two patches are not compared as text, because two correct patches can differ in their
    context lines. Both are applied to fresh copies of calc at base instead, and the calc tests
    must pass on the patched copy.
    """
    shown = f"output:\n{output}"
    assert DIFF_MARKER in output, shown
    diff = output.split(f"{DIFF_MARKER}\n", 1)[1]
    assert re.findall(r"^diff --git .*$", diff, re.MULTILINE) == [
        "diff --git a/src/calc/__init__.py b/src/calc/__init__.py"
    ], shown
    patch = tmp_path / "printed.patch"
    patch.write_text(diff.rstrip("\n") + "\n", encoding="utf-8")

    fixed = make_fixture_git(CALC, tmp_path / "fixed")
    reference = make_fixture_git(CALC, tmp_path / "reference")
    git(fixed, "apply", str(patch))
    git(reference, "apply", str(CALC / "REFERENCE.patch"))
    assert files_of(fixed) == files_of(reference)
    tests = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=fixed,
        env={**os.environ, "PYTHONPATH": str(fixed / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert tests.returncode == 0, tests.stdout
    assert "3 passed" in tests.stdout


# ---------- the tests ----------
def test_run_calc_durable(durable: Durable, tmp_path: Path) -> None:
    require_image()
    before = snapshot(CALC)
    worker = durable.start_worker()
    durable.wait_for_text(worker, "worker running")
    run = durable.start_run()

    code = run.wait(timeout=RUN_WAIT_S)

    shown = f"run:\n{durable.output_of(run)}\nworker:\n{durable.output_of(worker)}"
    assert code == 0, shown
    output = durable.output_of(run)
    assert "Traceback" not in output, shown
    task_id = durable.task_id_of(run)
    assert "finished after 5 turns: Changed add()" in output, shown
    assert "status: coding" in output, shown
    assert_the_reference_fix(output, tmp_path)
    assert snapshot(CALC) == before, "the source folder was changed"
    assert leftovers(task_id) == ([], [])


async def test_a_worker_stopped_with_sigint_mid_run_is_replaced_and_the_run_completes(
    durable: Durable, tmp_path: Path
) -> None:
    require_image()
    first = durable.start_worker()
    await asyncio.to_thread(durable.wait_for_text, first, "worker running")
    run = durable.start_run()
    task_id = await asyncio.to_thread(durable.task_id_of, run)

    settings = durable.settings
    client = await connect(settings["temporal"], namespace=settings["namespace"])
    handle = get_task(client, task_id)
    await wait_until_coding_past_the_edit(handle, durable, run)

    first.send_signal(signal.SIGINT)
    stopped = await asyncio.to_thread(first.wait, 180)
    assert stopped == 0, durable.output_of(first)
    assert "worker stopped" in durable.output_of(first)
    # With the worker gone nothing moves the run on. If it had already ended, this test would
    # not show a restart.
    assert (await handle.describe()).status is WorkflowExecutionStatus.RUNNING

    second = durable.start_worker()
    code = await asyncio.to_thread(run.wait, RUN_WAIT_S)

    shown = (
        f"run:\n{durable.output_of(run)}\nfirst worker:\n{durable.output_of(first)}\n"
        f"second worker:\n{durable.output_of(second)}"
    )
    assert code == 0, shown
    assert "finished after 5 turns" in durable.output_of(run), shown
    assert_the_reference_fix(durable.output_of(run), tmp_path)
    assert leftovers(task_id) == ([], [])


async def wait_until_coding_past_the_edit(
    handle: WorkflowHandle[Any, Any],
    durable: Durable,
    run: subprocess.Popen[str],
    seconds: float = 600,
) -> None:
    """Poll status() until the run has done three turns, so the edit is saved and tests are next.

    It gives up at once when the run command has ended, which it does when the workflow fails, for
    example because the sandbox setup could not reach PyPI. Polling a failed workflow would never
    show the phase change.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert run.poll() is None, (
            f"the run ended before the worker could be stopped:\n{durable.output_of(run)}"
        )
        try:
            status = await read_status(handle, timeout_s=5)
        except Exception:
            status = None  # the workflow has not had its first task yet
        if status is not None and status.phase in (Phase.DONE, Phase.FAILED, Phase.CANCELLED):
            pytest.fail(f"the run ended before the worker could be stopped: {status}")
        if status is not None and status.phase is Phase.CODING and status.turn_index >= 3:
            return
        await asyncio.sleep(0.2)
    pytest.fail(f"the run did not reach its third turn in {seconds:.0f} s")


def test_sigterm_stops_an_idle_worker_cleanly(durable: Durable) -> None:
    worker = durable.start_worker()
    durable.wait_for_text(worker, "worker running")

    worker.send_signal(signal.SIGTERM)
    code = worker.wait(timeout=60)

    assert code == 0, durable.output_of(worker)
    assert "worker stopped" in durable.output_of(worker)
