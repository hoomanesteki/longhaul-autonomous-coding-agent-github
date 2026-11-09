"""Run a seeded console: the stack behind the browser test of LH-808, and a demo to look at.

`scripts/seed_demo.sh` hands over to this file. It starts what the console needs and puts the
tasks in the states a person has to act on:

* `demo/calc-shipped`: a task that plans, fixes the bug and opens a pull request.
* `demo/calc-stuck`: a task that runs the failing suite three times and pauses for a hint.
* `demo/calc-plan`: a task whose repo asks for a decision on the plan, and waits for it.
* two lesson candidates in the memory of the first repo.

Nothing here is a stand-in for the product. GitHub is a FakeGitHub served over HTTP, the model is
the scripted provider with tests/fixtures/scripts/console_demo.yaml, and everything else is the
real thing: `longhaul worker` with Docker sandboxes, Temporal, Postgres, MinIO, the poller, and
`longhaul api` serving the built console. The services are the ones of `make up`, so `make up`,
`make sandbox-image` and `npm run build` in console/ come first. The workspace setup installs
pytest and calc from PyPI, so the machine needs outbound network.

The API starts last, once the tasks are in their states. A browser test that waits for its health
check therefore never sees a half-seeded stack. The process then stays up until it gets SIGINT or
SIGTERM, and removes what it made: the database, the sandboxes and their volumes, the blobs, the
open workflows and the folder of repos and checkpoints. It touches nothing else on the machine.
The database is a scratch one, so a demo never writes into the database of `make up`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
# The FakeGitHub lives with the tests, as `make fake-github` does.
sys.path.insert(0, str(ROOT))

CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
SCRIPT = ROOT / "tests" / "fixtures" / "scripts" / "console_demo.yaml"
CONFIG = ROOT / "config" / "longhaul.example.yaml"
DIST = ROOT / "console" / "dist"
IMAGE = "longhaul-sandbox:py312"
# `uv run` and `uv sync` put the script next to the interpreter.
LONGHAUL = Path(sys.executable).with_name("longhaul")
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_demo_"

# Not 8000, which the compose `api` service publishes. The browser test and the script read the
# same variable, LH_DEMO_PORT, and fall back to the same number.
DEFAULT_PORT = "8765"
# The token of a demo that listens on this machine only. LONGHAUL_ADMIN_TOKEN replaces it.
DEFAULT_TOKEN = "demo-admin-token"
# The label the poller looks for, and the login of the person who opened the issues.
LABEL = "longhaul"
GITHUB_TOKEN = "ghp_demo_console_0123456789abcdef"
# The file that makes a repo the stuck one. The scripted model reads only the messages of a
# request and not the system prompt, which is where the issue text goes, so console_demo.yaml tells
# the tasks apart by what the coder has read by its second turn: the listing of the repo root.
STUCK_MARKER = "SCENARIO_STUCK.md"

WORKER_START_S = 60
# Three sandboxes build a venv and install packages before the first model call, in parallel.
SEED_TIMEOUT_S = 420
API_START_S = 60


@dataclass(frozen=True)
class Scenario:
    """One task of the demo: its repo, its issue and what the repo asks of a person."""

    key: str
    repo: str
    title: str
    plan_approval_required: bool = False
    # A file this repo has and the others do not, for the script to tell the task by.
    marker_file: str | None = None


SCENARIOS = (
    Scenario("shipped", "demo/calc-shipped", "Fix add() so it sums"),
    Scenario("stuck", "demo/calc-stuck", "Fix add() after a hint", marker_file=STUCK_MARKER),
    Scenario(
        "plan", "demo/calc-plan", "Fix add() after a plan review", plan_approval_required=True
    ),
)

# Two notes for a person to decide on, as a task would propose them. They go into the memory of
# the first repo, with the shipped task as their source.
LESSONS = (
    "Run the whole suite with python -m pytest -q before you submit a fix.",
    "calc.add and calc.multiply both live in src/calc/__init__.py.",
)


def demo_config(base: dict[str, Any], pricing_file: Path) -> dict[str, Any]:
    """The example config with one allowlisted repo per scenario and the hash embedder.

    The example's first repo is the template, so the setup and test commands stay the ones that
    the product ships with. The local embedder needs the memory extra and a model download, and
    the hash one needs neither. The pricing file is a path relative to the checkout, which a copy
    elsewhere would lose, so it is made absolute.
    """
    template = base["repos"][0]
    config = {**base, "memory": {**base["memory"], "embedder": "hash"}}
    config["pricing_file"] = str(pricing_file)
    config["repos"] = [
        {
            **template,
            "repo": s.repo,
            "allowlisted": True,
            "plan_approval_required": s.plan_approval_required,
        }
        for s in SCENARIOS
    ]
    return config


def log(message: str) -> None:
    print(f"seed_demo: {message}", file=sys.stderr, flush=True)


def ready_message(demo: Demo) -> str:
    """The line that says where the console is and how to sign in.

    The token is printed only when it is the demo default, which protects nothing. A token from
    LONGHAUL_ADMIN_TOKEN is a real one, and this line reaches the terminal and the log that
    Playwright keeps, so it only says where the token comes from.
    """
    where = f"http://127.0.0.1:{demo.port}"
    if demo.token == DEFAULT_TOKEN:
        return f"ready: {where} with the token {demo.token}"
    return f"ready: {where} with the token from LONGHAUL_ADMIN_TOKEN"


def load_fixture_builder() -> Any:
    spec = importlib.util.spec_from_file_location(
        "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_fixture_git


def service_settings() -> dict[str, str]:
    """Where the compose services are. A .env that remaps a port applies here as it does to make."""
    from longhaul.settings import EnvSettings

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


class DemoError(RuntimeError):
    """The stack did not come up as described. The message says what to do."""


@dataclass
class Demo:
    """One run of the demo: its folder, database, processes and task ids, with their clean-up."""

    port: str
    token: str
    services: dict[str, str] = field(default_factory=service_settings)
    folder: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="longhaul-demo-")))
    queue: str = field(default_factory=lambda: f"lh-demo-{uuid.uuid4().hex[:8]}")
    # A task id is made of the repo and the issue number, and Temporal never forgets a workflow id
    # of an earlier run. A number of its own for each run keeps the fixed repo names usable.
    issue_number: int = field(default_factory=lambda: 100_000 + uuid.uuid4().int % 900_000)
    database_name: str | None = None
    database: str | None = None
    server: Any = None
    processes: list[subprocess.Popen[str]] = field(default_factory=list)
    task_ids: dict[str, str] = field(default_factory=dict)  # by scenario key

    # ---------- the checks that come first ----------
    def preflight(self) -> None:
        if not LONGHAUL.exists():
            raise DemoError(f"{LONGHAUL} is missing; run uv sync, which installs the script")
        if not (DIST / "index.html").is_file():
            raise DemoError("console/dist is missing; run npm --prefix console run build")
        found = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True)
        if found.returncode != 0:
            raise DemoError(f"image {IMAGE} is missing; run make sandbox-image first")

    # ---------- the database ----------
    def create_database(self) -> str:
        """A fresh database at the head revision, so the demo never writes into the real one."""
        import psycopg
        from alembic import command
        from alembic.config import Config
        from psycopg import sql
        from sqlalchemy.engine import make_url

        server = make_url(self.services["database"])
        admin = str(server.set(database="postgres").render_as_string(hide_password=False))
        name = SCRATCH_PREFIX + uuid.uuid4().hex[:12]
        url = str(server.set(database=name).render_as_string(hide_password=False))
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.database_name, self.database = name, url
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        return url

    def drop_database(self) -> None:
        if self.database_name is None:
            return
        import psycopg
        from psycopg import sql
        from sqlalchemy.engine import make_url

        server = make_url(self.services["database"])
        admin = str(server.set(database="postgres").render_as_string(hide_password=False))
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(self.database_name)
                )
            )

    # ---------- GitHub and the config ----------
    def start_github(self) -> None:
        """Three repos from the calc fixture, each with one labeled issue, behind a FakeGitHub."""
        from tests.fakes.fake_github import FakeGitHub, serve_in_thread

        make_fixture_git = load_fixture_builder()
        plain = make_fixture_git(CALC, self.folder / "source")
        issue_text = (CALC / "ISSUE.md").read_text(encoding="utf-8")
        fake = FakeGitHub()
        for scenario in SCENARIOS:
            source = plain
            if scenario.marker_file is not None:
                variant = self.folder / f"variant-{scenario.key}"
                shutil.copytree(CALC, variant, ignore=shutil.ignore_patterns("REFERENCE.patch"))
                (variant / scenario.marker_file).write_text(
                    "The console demo gets this copy of calc stuck on its first try.\n",
                    encoding="utf-8",
                )
                source = make_fixture_git(variant, self.folder / f"source-{scenario.key}")
            owner, _, name = scenario.repo.partition("/")
            remote = self.remotes / owner / f"{name}.git"
            fake.seed_repo(scenario.repo, remote=remote, source=source)
            fake.seed_issue(
                scenario.repo,
                scenario.title,
                body=issue_text,
                labels=[LABEL],
                number=self.issue_number,
            )
        self.fake = fake
        self.server = serve_in_thread(fake)

    @property
    def remotes(self) -> Path:
        """The folder LONGHAUL_GITHUB_WEB_URL names. A repo's remote is `<owner>/<name>.git`."""
        return self.folder / "remotes"

    def write_config(self) -> Path:
        base = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        path = self.folder / "longhaul.yaml"
        path.write_text(
            yaml.safe_dump(demo_config(base, ROOT / base["pricing_file"])), encoding="utf-8"
        )
        return path

    # ---------- the processes ----------
    def environment(self, config: Path) -> dict[str, str]:
        """The machine's environment without any longhaul setting or model key, plus this demo."""
        drop = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"}
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("LONGHAUL_") and k not in drop
        }
        # The console script finds the package through the editable install's .pth file, which
        # Python on macOS skips when the file carries the hidden flag. Name src explicitly.
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")])
        )
        s = self.services
        assert self.database is not None and self.server is not None
        env.update(
            LONGHAUL_DATABASE_URL=self.database,
            LONGHAUL_TEMPORAL_ADDRESS=s["temporal"],
            LONGHAUL_TEMPORAL_NAMESPACE=s["namespace"],
            LONGHAUL_TASK_QUEUE=self.queue,
            LONGHAUL_BLOB_ENDPOINT=s["blob_endpoint"],
            LONGHAUL_BLOB_BUCKET=s["blob_bucket"],
            LONGHAUL_BLOB_ACCESS_KEY=s["blob_access_key"],
            LONGHAUL_BLOB_SECRET_KEY=s["blob_secret_key"],
            LONGHAUL_CKPT_DIR=str(self.folder / "ckpt"),
            LONGHAUL_CONFIG=str(config),
            LONGHAUL_PROVIDER="scripted",
            LONGHAUL_SCRIPT=str(SCRIPT),
            LONGHAUL_GITHUB_TOKEN=GITHUB_TOKEN,
            LONGHAUL_GITHUB_API_URL=self.server.url,
            LONGHAUL_GITHUB_WEB_URL=str(self.remotes),
            # Port 0 lets the system pick a free port. The default is taken by the worker of a
            # developer or of another run on the same machine.
            LONGHAUL_METRICS_PORT="0",
        )
        return env

    def spawn(self, name: str, args: list[str], env: dict[str, str]) -> Path:
        """Start `longhaul <args>` in its own session, with its output in a file of the folder."""
        log_file = self.folder / f"{name}.log"
        with log_file.open("w") as out:
            proc = subprocess.Popen(
                [str(LONGHAUL), *args],
                cwd=self.folder,
                env=env,
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        self.processes.append(proc)
        return log_file

    def output(self, name: str) -> str:
        path = self.folder / f"{name}.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    async def start_worker(self, env: dict[str, str]) -> None:
        self.spawn("worker", ["worker"], env)
        proc = self.processes[-1]
        deadline = time.monotonic() + WORKER_START_S
        while "worker running" not in self.output("worker"):
            if proc.poll() is not None:
                raise DemoError(f"the worker ended first:\n{self.output('worker')}")
            if time.monotonic() > deadline:
                raise DemoError(f"the worker did not start in {WORKER_START_S} s")
            await asyncio.sleep(0.2)

    async def start_api(self, env: dict[str, str]) -> None:
        env = {**env, "LONGHAUL_ADMIN_TOKEN": self.token, "LONGHAUL_CONSOLE_DIR": str(DIST)}
        self.spawn("api", ["api", "--host", "127.0.0.1", "--port", self.port], env)
        proc = self.processes[-1]
        url = f"http://127.0.0.1:{self.port}/healthz"
        deadline = time.monotonic() + API_START_S
        while not await asyncio.to_thread(answers, url):
            if proc.poll() is not None:
                raise DemoError(
                    f"the API ended first (is port {self.port} taken?):\n{self.output('api')}"
                )
            if time.monotonic() > deadline:
                raise DemoError(f"the API did not answer in {API_START_S} s:\n{self.output('api')}")
            await asyncio.sleep(0.3)

    # ---------- the tasks ----------
    async def seed_tasks(self) -> None:
        """Poll once, then wait until each task is in the state its scenario describes."""
        from temporalio.service import RPCError

        from longhaul.contracts import OutcomeKind, Phase
        from longhaul.durable.client import connect, get_task, read_status
        from longhaul.durable.workflows.poller import PollerWorkflow

        s = self.services
        client = await connect(s["temporal"], namespace=s["namespace"])
        polled = await client.execute_workflow(
            PollerWorkflow.run, id=f"lh-demo-poll-{uuid.uuid4().hex[:8]}", task_queue=self.queue
        )
        # The ids come first: close() ends the workflows and removes the sandboxes by id, and a
        # poll that started the wrong number of tasks still started some.
        self.task_ids = await asyncio.to_thread(self.read_task_ids)
        if polled.started != len(SCENARIOS):
            raise DemoError(f"the poller started {polled.started} tasks, not {len(SCENARIOS)}")
        missing = [s.key for s in SCENARIOS if s.key not in self.task_ids]
        if missing:
            raise DemoError(f"no task row for {', '.join(missing)}")
        log(f"started {', '.join(self.task_ids.values())}")

        async def reaches(key: str, phase: Phase) -> None:
            handle = get_task(client, self.task_ids[key])
            while True:
                try:
                    status = await read_status(handle, timeout_s=10)
                except RPCError:
                    # A query waits for a worker to replay the workflow, which takes a while
                    # when three tasks start at once. A query that fails at once must not spin.
                    await asyncio.sleep(1)
                    continue
                if status.phase is phase:
                    return
                if status.phase in (Phase.DONE, Phase.FAILED, Phase.CANCELLED):
                    raise DemoError(f"task {key} ended in {status.phase} before {phase}")
                await asyncio.sleep(1)

        async def ships() -> None:
            outcome = await get_task(client, self.task_ids["shipped"]).result()
            if outcome.kind is not OutcomeKind.PR_OPENED:
                raise DemoError(f"the shipped task ended as {outcome.kind}: {outcome.summary}")

        async def asks() -> None:
            await reaches("stuck", Phase.PAUSED)
            # The question and the label come after the phase, and a person acts on the question.
            repo, issue = SCENARIOS[1].repo, self.issue_number
            while "longhaul:needs-human" not in self.fake.label_names(repo, issue):
                await asyncio.sleep(0.5)

        try:
            async with asyncio.timeout(SEED_TIMEOUT_S):
                await asyncio.gather(ships(), asks(), reaches("plan", Phase.AWAITING_PLAN_APPROVAL))
        except TimeoutError as exc:
            raise DemoError(f"the tasks did not reach their states in {SEED_TIMEOUT_S} s") from exc

    def read_task_ids(self) -> dict[str, str]:
        import psycopg

        assert self.database is not None
        with psycopg.connect(self.database) as conn:
            found = conn.execute("SELECT repo, id FROM tasks").fetchall()
        rows = {str(repo): str(task_id) for repo, task_id in found}
        return {s.key: rows[s.repo] for s in SCENARIOS if s.repo in rows}

    async def seed_lessons(self) -> None:
        """Two candidates in the memory of the first repo, as a finished task would propose them."""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from longhaul.contracts import MemoryItem
        from longhaul.db.session import make_engine
        from longhaul.memory.embedder import HashEmbedder
        from longhaul.memory.store import PgMemoryStore

        assert self.database is not None
        engine = make_engine(self.database, pooled=False)
        try:
            sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
            store = PgMemoryStore(sessions, HashEmbedder())
            for lesson in LESSONS:
                item = MemoryItem(
                    id="",
                    repo=SCENARIOS[0].repo,
                    kind="lesson",
                    content=lesson,
                    status="candidate",
                    source="task",
                )
                await store.add_candidate(item, source_ref=self.task_ids["shipped"])
        finally:
            await engine.dispose()

    # ---------- the clean-up ----------
    async def close(self) -> None:
        """Remove everything this run made. A step may fail without stopping the rest."""
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        with contextlib.suppress(Exception):
            async with asyncio.timeout(30):
                await self.terminate_workflows()
        for task_id in self.task_ids.values():
            with contextlib.suppress(Exception):
                self.remove_sandbox(task_id)
            with contextlib.suppress(Exception):
                self.remove_blobs(task_id)
        if self.server is not None:
            with contextlib.suppress(Exception):
                self.server.stop()
        with contextlib.suppress(Exception):
            self.drop_database()
        shutil.rmtree(self.folder, ignore_errors=True)

    async def terminate_workflows(self) -> None:
        from temporalio.client import WorkflowExecutionStatus

        from longhaul.durable.client import connect, get_task

        client = await connect(self.services["temporal"], namespace=self.services["namespace"])
        for task_id in self.task_ids.values():
            handle = get_task(client, task_id)
            if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                await handle.terminate(reason="the demo ended")

    @staticmethod
    def remove_sandbox(task_id: str) -> None:
        import docker

        client = docker.from_env()
        label = {"label": f"longhaul.task_id={task_id}"}
        for container in client.containers.list(all=True, filters=label):
            container.remove(force=True)
        for volume in client.volumes.list(filters={"name": f"lh-ws-{task_id}"}):
            volume.remove(force=True)

    def remove_blobs(self, task_id: str) -> None:
        import boto3

        s = self.services
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


def answers(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


async def serve(demo: Demo) -> int:
    demo.preflight()
    log("creating a scratch database")
    # Alembic starts an event loop of its own, so it cannot run inside this one.
    await asyncio.to_thread(demo.create_database)
    log("starting FakeGitHub and the worker")
    await asyncio.to_thread(demo.start_github)
    env = demo.environment(demo.write_config())
    await demo.start_worker(env)
    log(f"polling; the tasks take a minute or two to reach their states (queue {demo.queue})")
    await demo.seed_tasks()
    await demo.seed_lessons()
    await demo.start_api(env)
    log(ready_message(demo))
    log("press ctrl-c to stop and remove everything this run made")
    await asyncio.Event().wait()  # until a signal cancels the task
    return 0


async def main_async(demo: Demo) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    assert task is not None
    stopping = False

    def stop() -> None:
        # The group signal of a test runner can arrive twice, and the clean-up must not be cut.
        nonlocal stopping
        if not stopping:
            stopping = True
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop)
    try:
        return await serve(demo)
    except asyncio.CancelledError:
        log("stopping")
        return 0
    except DemoError as exc:
        # The worker's log goes with the folder, so the end of it is printed with the error.
        log(f"error: {exc}\nworker log, last lines:\n{demo.output('worker')[-4000:]}")
        return 1
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: None)
        await demo.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve a console with three seeded tasks.")
    parser.add_argument("--port", default=os.environ.get("LH_DEMO_PORT") or DEFAULT_PORT)
    args = parser.parse_args(argv)
    token = os.environ.get("LONGHAUL_ADMIN_TOKEN") or DEFAULT_TOKEN
    demo = Demo(port=str(args.port), token=token)
    return asyncio.run(main_async(demo))


if __name__ == "__main__":
    raise SystemExit(main())
