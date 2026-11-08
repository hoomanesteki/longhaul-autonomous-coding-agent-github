"""A labeled issue becomes a pull request, on the whole stack (LH-308).

GitHub is a FakeGitHub served over HTTP, on port 8099 when that port is free (`make fake-github`
uses the same one) and on a free port otherwise, which the test says in its output. The worker is
`longhaul worker`, started the way an operator starts it, against the compose Temporal, Postgres
and MinIO and the local Docker daemon. It reaches the fake through LONGHAUL_GITHUB_API_URL, and
clones from and pushes to a bare repo on disk through LONGHAUL_GITHUB_WEB_URL. The model is the
scripted provider with a script that plans and then fixes `calc`, since every task the poller
starts plans first (LH-501). Run `make up` and `make sandbox-image` first.
The workspace setup builds a venv and installs pytest and calc from PyPI, so the machine needs
outbound network.

The poller runs once, as the Schedule would start it. The test then follows the task to its end
and checks what the issue shows: one "Picked up" comment, one comment with the plan, one final
comment with the link to the pull request, one pull request that a verifier passed and is ready
for review, and the label `longhaul:pr-open` where `longhaul:working` was.

Nothing here touches what other work on the machine uses. The test has its own task queue, its
own scratch database (migrated to head, then dropped), its own checkpoint folder, and a repo with
a random suffix, so its task id is its own. At the end it removes the containers and volumes of
its task, their blobs, and the workflow if it is still open.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib.util
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import docker
import psycopg
import pytest
import yaml
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url
from temporalio.client import WorkflowExecutionStatus
from tests.fakes.fake_github import FakeGitHub, RunningServer, serve_in_thread

from longhaul.contracts import OutcomeKind
from longhaul.durable import acts
from longhaul.durable.client import connect, get_task
from longhaul.durable.workflows.poller import PollerWorkflow
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.e2e, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
SCRIPT = ROOT / "tests" / "fixtures" / "scripts" / "calc_fix_planned.yaml"
CONFIG = ROOT / "config" / "longhaul.example.yaml"
IMAGE = "longhaul-sandbox:py312"
# `uv run` and `uv sync` put the script next to the interpreter.
LONGHAUL = Path(sys.executable).with_name("longhaul")
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_e2e_pr_"
FAKE_GITHUB_PORT = 8099
LABEL = "longhaul"
TOKEN = "ghp_e2e_issue_to_pr_0123456789abcdef"
# The whole path from the labeled issue to the pull request has to fit in three minutes (LH-308).
# One deadline covers the start of the worker, both polls and the wait for the task, so slow
# stages cannot add up to more. The task itself is eight turns of a script (three of the planner,
# five of the coder) and a workspace setup, so what is left over is slack for a slow machine. The
# worker also has its own limit, because its start is a loop in a thread.
TOTAL_S = 180
WORKER_START_S = 60

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git


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


def serve_fake(fake: FakeGitHub) -> RunningServer:
    """Serve the fake on port 8099, or on a free port when something else holds 8099."""
    try:
        return serve_in_thread(fake, port=FAKE_GITHUB_PORT)
    except RuntimeError:
        server = serve_in_thread(fake)
        print(
            f"port {FAKE_GITHUB_PORT} is taken, so FakeGitHub is served on port {server.port}",
            file=sys.stderr,
        )
        return server


@dataclass
class Stack:
    """One test's FakeGitHub, worker and scratch state, with their clean-up."""

    tmp_path: Path
    database: str
    fake: FakeGitHub
    server: RunningServer
    repo: str
    queue: str = field(default_factory=lambda: f"lh-e2e-pr-{uuid.uuid4().hex[:8]}")
    # The script the worker plays. tests/e2e/test_stuck_flow.py runs the same stack with another.
    script: Path = SCRIPT
    processes: list[subprocess.Popen[str]] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)

    @property
    def remotes(self) -> Path:
        """The folder LONGHAUL_GITHUB_WEB_URL names. A repo's remote is `<owner>/<name>.git`."""
        return self.tmp_path / "remotes"

    @property
    def config_file(self) -> Path:
        return self.tmp_path / "longhaul.yaml"

    def write_config(self) -> None:
        """The example config with the one repo of this test in the allowlist."""
        config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        # The example repo holds every plan for a person. This test has none to answer, and the
        # gate has its own tests (tests/integration/test_plan_gate.py).
        config["repos"] = [
            {**config["repos"][0], "repo": self.repo, "plan_approval_required": False}
        ]
        # The local embedder needs the memory extra and a model download (LH-604). The hash one
        # needs neither, and the worker still keeps its memory.
        config["memory"]["embedder"] = "hash"
        self.config_file.write_text(yaml.safe_dump(config), encoding="utf-8")

    def environment(self) -> dict[str, str]:
        """The machine's environment without any longhaul setting or model key, plus this stack."""
        drop = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"}
        env = {
            k: v for k, v in os.environ.items() if not k.startswith("LONGHAUL_") and k not in drop
        }
        # The console script finds the package through the editable install's .pth file, which
        # Python on macOS skips when the file carries the hidden flag. Name src explicitly.
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, [str(ROOT / "src"), env.get("PYTHONPATH")])
        )
        s = service_settings()
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
            LONGHAUL_CONFIG=str(self.config_file),
            LONGHAUL_PROVIDER="scripted",
            LONGHAUL_SCRIPT=str(self.script),
            LONGHAUL_GITHUB_TOKEN=TOKEN,
            LONGHAUL_GITHUB_API_URL=self.server.url,
            LONGHAUL_GITHUB_WEB_URL=str(self.remotes),
            # Port 0 lets the system pick a free port for the metrics endpoint. The default is
            # taken by the worker of a developer or of another test run on the same machine.
            LONGHAUL_METRICS_PORT="0",
        )
        return env

    def start_worker(self) -> subprocess.Popen[str]:
        require_script()
        log = self.tmp_path / "worker.log"
        with log.open("w") as out:
            proc = subprocess.Popen(
                [str(LONGHAUL), "worker"],
                cwd=self.tmp_path,
                env=self.environment(),
                stdout=out,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        self.processes.append(proc)
        return proc

    def worker_log(self) -> str:
        log = self.tmp_path / "worker.log"
        return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""

    def wait_until_ready(self, proc: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + WORKER_START_S
        while time.monotonic() < deadline:
            if "worker running" in self.worker_log():
                return
            assert proc.poll() is None, f"the worker ended first:\n{self.worker_log()}"
            time.sleep(0.2)
        pytest.fail(f"the worker did not start in {WORKER_START_S} s:\n{self.worker_log()}")

    def close(self) -> None:
        for proc in self.processes:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
        self.server.stop()
        for task_id in self.task_ids:
            self.remove_sandbox(task_id)
            self.remove_blobs(task_id)
        self.terminate_workflows()

    @staticmethod
    def remove_sandbox(task_id: str) -> None:
        client = docker.from_env()
        label = {"label": f"longhaul.task_id={task_id}"}
        for container in client.containers.list(all=True, filters=label):
            container.remove(force=True)
        for volume in client.volumes.list(filters={"name": f"lh-ws-{task_id}"}):
            volume.remove(force=True)

    @staticmethod
    def remove_blobs(task_id: str) -> None:
        s = service_settings()
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

    def terminate_workflows(self) -> None:
        async def terminate() -> None:
            s = service_settings()
            client = await connect(s["temporal"], namespace=s["namespace"])
            for task_id in self.task_ids:
                handle = get_task(client, task_id)
                if (await handle.describe()).status is WorkflowExecutionStatus.RUNNING:
                    await handle.terminate(reason="the test ended")

        # Best effort: the server may be the thing that failed.
        with contextlib.suppress(Exception):
            asyncio.run(terminate())


@pytest.fixture
def stack(tmp_path: Path, database: str) -> Iterator[Stack]:
    suffix = uuid.uuid4().hex[:8]
    repo = f"acme/calc-{suffix}"
    fake = FakeGitHub()
    source = make_fixture_git(CALC, tmp_path / "source")
    owner, _, name = repo.partition("/")
    fake.seed_repo(repo, remote=tmp_path / "remotes" / owner / f"{name}.git", source=source)
    made = Stack(tmp_path, database, fake, serve_fake(fake), repo)
    made.write_config()
    try:
        yield made
    finally:
        made.close()


async def test_label_to_pr_fake_github(stack: Stack) -> None:
    require_image()
    try:
        async with asyncio.timeout(TOTAL_S):
            await label_to_pr(stack)
    except TimeoutError:
        pytest.fail(f"no pull request in {TOTAL_S} s, the target:\n{stack.worker_log()}")


async def label_to_pr(stack: Stack) -> None:
    fake, repo = stack.fake, stack.repo
    issue = fake.seed_issue(
        repo,
        "add() returns the difference",
        body=(CALC / "ISSUE.md").read_text(encoding="utf-8"),
        labels=[LABEL],
    )
    task_id = f"lh-acme-calc-{repo.rsplit('-', 1)[1]}-{issue}"
    stack.task_ids.append(task_id)
    worker = stack.start_worker()
    await asyncio.to_thread(stack.wait_until_ready, worker)
    s = service_settings()
    client = await connect(s["temporal"], namespace=s["namespace"])

    poll = await client.execute_workflow(
        PollerWorkflow.run,
        id=f"lh-e2e-poll-{uuid.uuid4().hex[:8]}",
        task_queue=stack.queue,
    )
    outcome = await get_task(client, task_id).result()

    shown = f"worker:\n{stack.worker_log()}"
    assert poll == acts.PollResult(repos=1, issues=1, started=1, known=0), shown
    assert outcome.kind is OutcomeKind.PR_OPENED, shown
    assert outcome.pr_url, shown

    # Exactly one pull request that closes the issue, from the branch of the task. The verifier
    # passed the work and the pre-check ran the test command, so it is ready for review (LH-504).
    (pull,) = fake.pulls(repo)
    assert pull["html_url"] == outcome.pr_url
    assert pull["draft"] is False
    assert "Verified: 3 passed" in pull["body"], pull["body"]
    assert pull["head"]["ref"] == f"longhaul-pr/{issue}"
    assert f"Closes #{issue}" in pull["body"], pull["body"]
    assert "3 passed" in pull["body"], pull["body"]
    # The run report from the tables is in the body (LH-705).
    assert "<summary>Run report</summary>" in pull["body"], pull["body"]
    assert "### Overview" in pull["body"], pull["body"]
    assert set(fake.branches(repo)) == {"main", f"longhaul-pr/{issue}"}, "main was not pushed to"

    # The issue shows one "Picked up" comment, the plan once, and the final comment with the link
    # to the pull request (LH-507), and nothing else from us.
    comments = fake.comment_bodies(repo, issue)
    assert len(comments) == 3, comments
    assert comments[0].startswith("Picked up"), comments[0]
    assert task_id in comments[0]
    assert "## Plan, version 1" in comments[1], comments[1]
    assert f"<!-- longhaul:key={task_id}:plan_comment:1 -->" in comments[1], comments[1]
    assert comments[2].startswith("## Longhaul opened a pull request\n"), comments[2]
    assert str(outcome.pr_url) in comments[2], comments[2]
    assert f"<!-- longhaul:key={task_id}:final:1 -->" in comments[2], comments[2]

    # `longhaul:working` came and went, and the pull request label stayed.
    assert sorted(fake.label_names(repo, issue)) == [LABEL, "longhaul:pr-open"]
    assert [(e.action, e.label) for e in fake.label_events(repo, issue)] == [
        ("labeled", "longhaul:working"),
        ("unlabeled", "longhaul:working"),
        ("labeled", "longhaul:pr-open"),
    ]

    # A second poll finds the issue still labeled, and starts nothing and opens nothing.
    again = await client.execute_workflow(
        PollerWorkflow.run,
        id=f"lh-e2e-poll-{uuid.uuid4().hex[:8]}",
        task_queue=stack.queue,
    )
    assert again == acts.PollResult(repos=1, issues=1, started=0, known=1), shown
    assert fake.count_pulls(repo) == 1
    assert fake.count_comments(repo, issue) == 3

    with psycopg.connect(stack.database) as conn:
        rows = conn.execute("SELECT id, repo, issue_number FROM tasks").fetchall()
    assert rows == [(task_id, repo, issue)]
