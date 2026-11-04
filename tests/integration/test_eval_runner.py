"""The eval runner and the custom grader on the real stack (LH-901).

Run `make up` and `make sandbox-image` first. The sandbox setup installs pytest and the repo
from PyPI, so the machine needs outbound network.

The runner test starts a worker in this process, on a task queue of its own, against a scratch
database that is migrated and dropped. The worker has no provider and no script: each task
brings its script, as `longhaul run --script` does. At the end the test removes what it made,
which is the containers and volumes of its tasks, their blobs and any workflow still open.
Other work on the machine keeps its own queue, database and containers.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import boto3
import docker
import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

from eval import custom_tasks, grade_custom, results, run
from longhaul.agent.workspace import prepare_local_workspace
from longhaul.durable import worker
from longhaul.durable.client import connect, get_task
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import EnvSettings, config_path, get_settings, load_config

pytestmark = [pytest.mark.integration, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "longhaul.example.yaml"
SCRIPTS = ROOT / "tests" / "fixtures" / "scripts" / "eval"
LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"
SCRATCH_PREFIX = "longhaul_eval_"
IMAGE = "longhaul-sandbox:py312"
WORKER_STOP_S = 90


# ---------- the services ----------
def service_settings(**changes: object) -> EnvSettings:
    """The compose services as the worker finds them, with the local defaults for the rest."""
    base = EnvSettings()
    access, secret = base.blob_access_key, base.blob_secret_key
    values: dict[str, object] = {
        "database_url": base.database_url or LOCAL_DSN,
        "temporal_address": base.temporal_address,
        "temporal_namespace": base.temporal_namespace,
        "blob_endpoint": base.blob_endpoint or "http://localhost:9000",
        "blob_bucket": base.blob_bucket or "longhaul",
        "blob_access_key": access or "longhaul",
        "blob_secret_key": secret or "longhaul-secret",
        "provider": "scripted",
        "config": CONFIG,
        **changes,
    }
    return EnvSettings(_env_file=None, **values)  # type: ignore[arg-type]


def require_image() -> None:
    try:
        docker.from_env().images.get(IMAGE)
    except docker.errors.ImageNotFound:
        pytest.fail(f"image {IMAGE} is missing; run make sandbox-image first")


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A fresh database at the head revision. Alembic must not reach the application database."""
    secret = EnvSettings().database_url
    server = make_url(secret.get_secret_value() if secret else LOCAL_DSN)
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
class EvalStack:
    settings: EnvSettings
    results_dir: Path
    run_id: str = field(default_factory=lambda: f"it{uuid.uuid4().hex[:8]}")

    @property
    def prefix(self) -> str:
        return f"lh-eval-{self.run_id}-"

    def options(self, *extra: str) -> run.RunOptions:
        return run.parse_args(
            [
                "--set",
                "custom",
                "--config",
                "full",
                "--run-id",
                self.run_id,
                "--results-dir",
                str(self.results_dir),
                *extra,
            ]
        )


@pytest.fixture
async def stack(tmp_path: Path, database: str) -> AsyncIterator[EvalStack]:
    require_image()
    queue = f"lh-eval-{uuid.uuid4().hex[:8]}"
    settings = service_settings(database_url=database, task_queue=queue, ckpt_dir=tmp_path / "ckpt")
    parts = worker.build_parts(settings, load_config(CONFIG), DockerSandboxManager())
    client = await connect(settings.temporal_address, namespace=settings.temporal_namespace)
    stop = asyncio.Event()
    serving = asyncio.create_task(worker.serve(client, queue, parts.activities, stop))
    current = EvalStack(settings, tmp_path / "results")
    try:
        yield current
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            async with asyncio.timeout(WORKER_STOP_S):
                await serving
        await parts.close()
        await remove_leftovers(current, client)


async def remove_leftovers(stack: EvalStack, client: object) -> None:
    """Remove the sandboxes, blobs and open workflows of this test's tasks, and nothing else."""
    docker_client = docker.from_env()
    tasks = {
        str(c.labels["longhaul.task_id"])
        for c in docker_client.containers.list(all=True, filters={"label": "longhaul.task_id"})
        if str(c.labels["longhaul.task_id"]).startswith(stack.prefix)
    }
    for container in docker_client.containers.list(all=True, filters={"label": "longhaul.task_id"}):
        if str(container.labels["longhaul.task_id"]).startswith(stack.prefix):
            container.remove(force=True)
    for volume in docker_client.volumes.list(filters={"name": f"lh-ws-{stack.prefix}"}):
        volume.remove(force=True)
    settings = stack.settings
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.blob_endpoint,
        aws_access_key_id=settings.blob_access_key.get_secret_value()
        if settings.blob_access_key
        else None,
        aws_secret_access_key=settings.blob_secret_key.get_secret_value()
        if settings.blob_secret_key
        else None,
        region_name="us-east-1",
    )
    listing = s3.list_objects_v2(Bucket=settings.blob_bucket, Prefix=f"tasks/{stack.prefix}")
    keys = [{"Key": item["Key"]} for item in listing.get("Contents", [])]
    if keys:
        s3.delete_objects(Bucket=settings.blob_bucket, Delete={"Objects": keys})
    for workflow_id in {*tasks, *(f"{stack.prefix}{t.id}" for t in custom_tasks.load_tasks())}:
        with contextlib.suppress(Exception):
            await get_task(client, workflow_id).terminate(reason="the test ended")  # type: ignore[arg-type]


# ---------- patches for the grader ----------
def patch_of(path: str, old: str, new: str) -> str:
    """A unified diff of one file, the way git apply wants it."""
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def calc_patch(*changes: tuple[str, str]) -> str:
    """The base `calc/__init__.py` with each (old, new) pair of lines replaced, as a patch."""
    path = "src/calc/__init__.py"
    old = (custom_tasks.REPOS_DIR / "calc" / path).read_text(encoding="utf-8")
    new = old
    for before, after in changes:
        assert before in new, before
        new = new.replace(before, after)
    return patch_of(path, old, new)


# ---------- the dry run ----------
async def test_dry_run_writes_results(stack: EvalStack) -> None:
    options = stack.options("--limit", "2", "--concurrency", "2", "--script", str(SCRIPTS))
    assert options.provider == "scripted"

    summary = await run.run_eval(options, stack.settings)

    folder = stack.results_dir / stack.run_id
    names = sorted(p.name for p in folder.iterdir())
    assert names == [
        "calc-01.json",
        "calc-01.patch",
        "calc-02.json",
        "calc-02.patch",
        "summary.json",
    ]
    found = results.read_results(folder)
    assert [r.task_id for r in found] == ["calc-01", "calc-02"]
    for result in found:
        assert result.run_id == stack.run_id
        assert result.workflow_id == f"{stack.prefix}{result.task_id}"
        assert (result.task_set, result.config) == ("custom", "full")
        assert result.model == load_config(config_path(stack.settings)).models.coder
        assert result.outcome == "pr_opened"
        # The scripts fix the bug, and the grader ran the hidden tests on the patch.
        assert result.resolved is True, result.grading
        assert result.turns == 5
        # A scripted run is free, but every call is on the ledger with its tokens.
        assert result.cost_usd == 0.0
        assert result.tokens.input > 0
        assert result.tokens.output > 0
        assert result.cache_ratio == 0.0
        assert result.wall_time_s > 0
        assert result.ended_at > result.started_at
        assert (result.compactions, result.verify_rounds) == (0, 0)
        assert result.stuck_reasons == []
        assert result.intervention_needed is False
        assert result.stopped_by_runner is None
    patch = (folder / "calc-01.patch").read_text(encoding="utf-8")
    assert "-    return a - b" in patch
    assert "+    return a + b" in patch

    on_disk = results.Summary.model_validate_json((folder / "summary.json").read_text("utf-8"))
    assert on_disk == summary
    assert (summary.attempted, summary.resolved, summary.resolve_rate) == (2, 2, 1.0)
    assert summary.tasks == ["calc-01", "calc-02"]
    assert summary.intervention_needed == 0


# ---------- the grader ----------
async def test_grader_good_bad(tmp_path: Path) -> None:
    require_image()
    task = {t.id: t for t in custom_tasks.load_tasks()}["calc-01"]
    grader = grade_custom.Grader(
        DockerSandboxManager(),
        image=load_config(CONFIG).sandbox.image,
        limits=load_config(CONFIG).sandbox.limits,
    )
    good = custom_tasks.reference_patch_path(task).read_text(encoding="utf-8")
    # Not a fix: the sum becomes a product, so the hidden tests fail.
    wrong = calc_patch(("return a - b", "return a * b"))
    # Fixes add() and breaks multiply(): the hidden tests pass, a repo test fails.
    breaks_repo_test = calc_patch(
        ("return a - b", "return a + b"), ("return a * b", "return a + b")
    )
    garbage = "--- a/nowhere.py\n+++ b/nowhere.py\n@@ -1 +1 @@\n-x\n+y\n"

    resolved = await grader.grade(task, good)
    assert resolved.resolved is True, resolved.output_tail
    assert (resolved.hidden_passed, resolved.hidden_failed, resolved.repo_failed) == (3, 0, 0)

    unresolved = await grader.grade(task, wrong)
    assert unresolved.resolved is False
    assert unresolved.hidden_failed == 3

    broken = await grader.grade(task, breaks_repo_test)
    assert broken.resolved is False
    assert (broken.hidden_passed, broken.hidden_failed, broken.repo_failed) == (3, 0, 1)
    assert "test_multiply" in broken.output_tail

    refused = await grader.grade(task, garbage)
    assert refused.resolved is False
    assert "apply" in refused.reason

    nothing = await grader.grade(task, "")
    assert nothing.resolved is False
    assert "empty" in nothing.reason


# ---------- what the agent can see ----------
async def test_hidden_tests_not_visible() -> None:
    require_image()
    tasks = custom_tasks.load_tasks()
    withheld = [custom_tasks.hidden_tests_path(t) for t in tasks]
    withheld += [custom_tasks.reference_patch_path(t) for t in tasks]
    digests = {hashlib.sha256(p.read_bytes()).hexdigest() for p in withheld}
    names = sorted({p.name for p in withheld})
    by_name = " -o ".join(f"-name '{name}'" for name in names)

    manager = DockerSandboxManager()
    cfg = load_config(CONFIG)
    for repo in sorted({t.repo for t in tasks}):
        task_id = f"lh-eval-vis-{uuid.uuid4().hex[:8]}"
        folder = custom_tasks.REPOS_DIR / repo
        custom_tasks.check_agent_view(folder)  # the guard the runner applies to every task
        try:
            sandbox, _ = await prepare_local_workspace(
                manager,
                folder,
                task_id=task_id,
                image=cfg.sandbox.image,
                limits=cfg.sandbox.limits,
                setup_command="",
            )
            by_content = await sandbox.exec(
                "find /workspace -path /workspace/.git -prune -o -type f -exec sha256sum {} +",
                timeout_s=60,
            )
            by_name_hits = await sandbox.exec(
                f"find / -xdev \\( {by_name} \\) 2>/dev/null; true", timeout_s=120
            )
            tracked = await sandbox.exec("git ls-tree -r --name-only HEAD", timeout_s=30)
        finally:
            await manager.cleanup_task(task_id)

        assert by_content.exit_code == 0, by_content.stderr
        copied = {line.split()[0] for line in by_content.stdout.splitlines() if line.strip()}
        assert copied, "the workspace holds no files, so the check proved nothing"
        assert not copied & digests, f"{repo}: a hidden file is in the workspace"
        assert by_name_hits.stdout.strip() == "", by_name_hits.stdout
        assert tracked.exit_code == 0, tracked.stderr
        listed = set(tracked.stdout.split())
        assert listed, "the base commit holds no files"
        assert not listed & set(names), listed
        assert not any("hidden" in item or "reference" in item for item in listed), listed
