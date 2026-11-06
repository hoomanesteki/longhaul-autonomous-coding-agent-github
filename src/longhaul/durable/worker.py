"""The worker process: `longhaul worker`.

A worker polls one Temporal task queue and runs the activities of TaskWorkflow and PollerWorkflow.
It is the only process that holds model keys, talks to Docker, and writes the database. The
workflows run in the worker too, under the sandbox settings of durable/workflow_sandbox.py.

What it is built from, and why each piece is what it is:

* The blob store is S3 (MinIO locally), never the in-memory one. `longhaul run` and a second
  worker are other processes, and they must read the blobs this one writes.
* The checkpoint store keeps its bare repos under LONGHAUL_CKPT_DIR on this host (ADR-0006). A
  worker that restarts finds them again, which is how a task survives it.
* The model provider is the one in LONGHAUL_PROVIDER. The scripted provider is the exception: a
  script belongs to a task and not to the worker (durable/scripts.py), so a scripted worker with
  no LONGHAUL_SCRIPT has no provider of its own and plays the script each task brings. The replay
  provider plays the recordings in LONGHAUL_RECORDINGS_DIR, which is how the chaos runs get the
  same model answers every time. With LONGHAUL_RECORD on, the provider is wrapped and saves each
  call to that folder (providers/replay.py).
* Every model call goes on the cost ledger (invariant I6), priced from `pricing_file`. A model
  in the config without a price stops the worker before it takes a task (load_pricing).
* Spans go to the OpenTelemetry collector when OTEL_EXPORTER_OTLP_ENDPOINT is set. The client
  carries the tracing interceptor, so the worker built on it needs no list of its own.
* GitHub repos need LONGHAUL_GITHUB_TOKEN, a fine-grained PAT (ADR-0012). The worker clones and
  pushes with it on the host; it never enters a sandbox (ADR-0009). Without it the worker takes
  local folders only and still recovers the sandbox of a GitHub task.
* Every activity logs when it starts, ends or fails (durable/activity_log.py). After a SIGKILL
  that is all a worker leaves behind, and it says where the kill landed.
* With the token the worker also registers the GitHub writes (post_comment, add_label,
  remove_label, create_pr) and the delivery (build_pr_branch, deliver), LH-306. They share one
  idempotency ledger, the `side_effects` table on this worker's database, so a retry of any of
  them finds the row of the attempt before. The commits of a pull request branch are made by
  LONGHAUL_GIT_AUTHOR, and the body of the pull request comes from the templates folder
  (LONGHAUL_TEMPLATES_DIR). Without the token none of them is registered, and a task of an
  issue could not have been prepared in the first place.
* Metrics are served at /metrics on LONGHAUL_METRICS_PORT (default 9464, which Prometheus in
  docker/prometheus/prometheus.yml scrapes) from before the worker connects until it stops. The
  activities update them (telemetry/metrics.py), and the interceptor that `serve` gives the
  worker counts the retries of every activity. A port that is taken stops the worker at the
  start, with the name of the setting to change.
* The tools are the default set. `run_tests` has one test command for the whole worker, because
  the workflow does not pass the task's settings to `tool_exec`.
* The poller (LH-307) reads GitHub with LONGHAUL_GITHUB_TOKEN and starts tasks on this worker's
  own queue. Without a token the worker still starts and still registers `poll_once`, so a
  worker for local runs needs none. A poll on it fails with the name of the setting, and the
  Temporal UI shows that. The Schedule that triggers the polls is made by `longhaul poller
  install` and not by the worker.
* The two context tiers follow the `context` section of the config (LH-404): `llm_turn` elides old
  tool outputs and decides when the view must be compacted, and `compact` does it with the
  compactor model. They share one set of token meters, so both count with the ratio that the
  replies of a task taught. The compactor's calls are on the cost ledger one by one (I6).
  `context.mode` can switch the tiers off (LH-406), and a run can name its own mode. A run that
  names none takes the config of whichever worker runs each of its turns, so the workers of a
  queue should share one `context.mode`.

Stopping. SIGTERM and SIGINT start a graceful stop: the worker polls no more, waits up to
GRACEFUL_SHUTDOWN_S for the activities that are running, and cancels the rest. A cancelled
activity is not reported, so Temporal notices after its heartbeat timeout and runs it again on the
next worker. `tool_exec` restores `pre_sha` first (ADR-0006), so this is safe at any moment. The
same happens, without the waiting, when the process is killed. A second signal changes nothing:
the wait is short and bounded.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.agent.workspace import DEFAULT_TEST_COMMAND
from longhaul.blobs.s3 import S3BlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import LLMProvider, Role
from longhaul.cost.ledger import SqlLedger
from longhaul.cost.pricing import load_pricing
from longhaul.db.records import SqlRecorder
from longhaul.db.session import make_engine
from longhaul.db.tasks import SqlTaskStore
from longhaul.durable.activities.context import ContextActivities
from longhaul.durable.activities.delivery import DeliveryActivities
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.github import PollerActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.support import Meters
from longhaul.durable.activities.tools import ToolActivities
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.durable.activity_log import ActivityLogInterceptor
from longhaul.durable.client import connect
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.poller import PollerWorkflow
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github.client import GITHUB_API_URL, GitHubClient, PatAuth
from longhaul.github.remote import GitHubRemote, clone_url_under, github_clone_url
from longhaul.github.side_effects import SideEffectActivities
from longhaul.github.side_effects import SqlLedger as EffectLedger
from longhaul.providers.base import get_provider
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import AppConfig, EnvSettings, config_path, load_config, templates_path
from longhaul.telemetry import metrics
from longhaul.telemetry.otel import setup_tracing
from longhaul.tools.registry import default_registry

log = logging.getLogger(__name__)

# How long a stop waits for running activities. A model turn and most tool calls finish in this
# time. A test run that takes longer is cancelled and runs again on the next worker.
GRACEFUL_SHUTDOWN_S = 30

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


@dataclass
class WorkerParts:
    """What `build_parts` made: the activities to register, and what to close at the end."""

    activities: list[Callable[..., Any]]
    blobs: S3BlobStore
    store: GitCheckpointStore
    engine: AsyncEngine
    github: GitHubClient | None = None

    async def close(self) -> None:
        """Close the database pool and the GitHub client. The others hold no open connection."""
        await self.engine.dispose()
        if self.github is not None:
            await self.github.aclose()


def build_provider(settings: EnvSettings) -> LLMProvider | None:
    """The provider for tasks that bring no script, or None for a scripted worker without one.

    Any other missing setting, such as the key of a real provider or the folder of a replay,
    raises ValueError, so a misconfigured worker stops at the start and not at its first model
    turn.
    """
    if settings.provider == "scripted" and settings.script is None:
        if settings.record:
            raise ValueError(
                "LONGHAUL_RECORD has no provider to record: LONGHAUL_PROVIDER is scripted and "
                "LONGHAUL_SCRIPT is not set, so each task brings its own script. Set "
                "LONGHAUL_SCRIPT, or use a provider of the worker"
            )
        return None
    return get_provider(settings)


def clone_url_for(settings: EnvSettings) -> Callable[[str], str]:
    """Where repos are cloned from: github.com, or the base the settings name."""
    base = settings.github_clone_base
    return github_clone_url if base is None else clone_url_under(base)


def build_parts(
    settings: EnvSettings, cfg: AppConfig, sandboxes: DockerSandboxManager
) -> WorkerParts:
    """The activities of a worker, wired to the real stores. It opens no connection.

    `sandboxes` is a DockerSandboxManager. It is a parameter because building one connects to
    the Docker daemon, which a test of this wiring does not have.
    """
    if settings.database_url is None:
        raise ValueError("LONGHAUL_DATABASE_URL is not set")
    try:
        blobs = S3BlobStore.from_settings(settings)
    except RuntimeError as exc:  # a setting is unset; the command reports it in one line
        raise ValueError(str(exc)) from exc
    store = GitCheckpointStore(blobs, root=settings.ckpt_dir)
    engine = make_engine(settings.database_url.get_secret_value())
    sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    recorder = SqlRecorder(sessions)
    ledger = SqlLedger(sessions, load_pricing(settings.provider, cfg))
    # read_output reads the handles tool_exec writes, so the tools share the worker's blob store.
    registry = default_registry(
        test_command=DEFAULT_TEST_COMMAND,
        max_timeout_s=cfg.sandbox.command_timeout_s,
        blobs=blobs,
    )

    def config_for(role: Role) -> LoopConfig:
        return LoopConfig.from_config(cfg, role)

    provider = build_provider(settings)
    meters = Meters()
    llm = LlmActivities(
        provider,
        blobs,
        recorder,
        registry.specs(),
        config_for,
        ledger=ledger,
        context=cfg.context,
        meters=meters,
    )
    compactor = ContextActivities(
        provider,
        blobs,
        registry.specs(),
        config_for,
        cfg.context,
        ledger=ledger,
        meters=meters,
    )
    tools = ToolActivities(
        registry,
        sandboxes,
        blobs,
        recorder,
        checkpoints=store,
        tool_output_max_tokens=cfg.context.tool_output_max_tokens,
    )
    github: GitHubClient | None = None
    remote: GitHubRemote | None = None
    if settings.github_token is not None:
        auth = PatAuth(settings.github_token)
        github = GitHubClient(settings.github_api_url or GITHUB_API_URL, auth)
        remote = GitHubRemote(github, auth, clone_url=clone_url_for(settings))
    workspace = WorkspaceActivities(
        sandboxes,
        store,
        recorder,
        image=cfg.sandbox.image,
        limits=cfg.sandbox.limits,
        github=remote,
    )
    events = EventActivities(recorder)
    # The poller reads through the same client that GitHubRemote clones and pushes with, so
    # WorkerParts.close has one connection pool to close.
    poller = PollerActivities(github, SqlTaskStore(sessions), cfg, task_queue=settings.task_queue)
    delivery: list[Callable[..., Any]] = []
    if github is not None and remote is not None:
        # One ledger for the writes and the delivery, so that a key is held and recorded once.
        effects = EffectLedger(sessions)
        delivery = [
            *SideEffectActivities(github, effects).activities,
            *DeliveryActivities(
                store,
                remote,
                github,
                effects,
                recorder,
                blobs,
                author=settings.git_author,
                templates=templates_path(settings),
            ).activities,
        ]
    return WorkerParts(
        activities=[
            *llm.activities,
            *compactor.activities,
            *tools.activities,
            *workspace.activities,
            *events.activities,
            *poller.activities,
            *delivery,
        ],
        blobs=blobs,
        store=store,
        engine=engine,
        github=github,
    )


def install_signal_handlers(stop: asyncio.Event) -> None:
    """Set `stop` on SIGTERM and SIGINT, so the loop below can shut the worker down in order."""
    loop = asyncio.get_running_loop()
    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)


async def serve(
    client: Client,
    task_queue: str,
    activities: Sequence[Callable[..., Any]],
    stop: asyncio.Event,
    *,
    grace: timedelta = timedelta(seconds=GRACEFUL_SHUTDOWN_S),
) -> None:
    """Run a worker on the queue until `stop` is set, then stop it gracefully.

    A fatal error of the worker, such as a lost connection that the SDK cannot recover, ends the
    wait at once and is raised from here.
    """
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[TaskWorkflow, PollerWorkflow],
        activities=activities,
        workflow_runner=workflow_runner(),
        graceful_shutdown_timeout=grace,
        interceptors=[ActivityLogInterceptor(), metrics.RetryCounter()],
    )
    running = asyncio.create_task(worker.run())
    stopping = asyncio.create_task(stop.wait())
    try:
        await asyncio.wait({running, stopping}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopping.cancel()
    if not running.done():
        log.info("stopping: waiting up to %s s for the activities that are running", grace.seconds)
        await worker.shutdown()
    await running


async def main() -> None:
    """Build the worker from the settings and run it until SIGTERM or SIGINT."""
    settings = EnvSettings()
    cfg = load_config(config_path(settings))
    parts = build_parts(settings, cfg, DockerSandboxManager())
    # Before connect: the client takes the tracing interceptor when tracing is set up.
    tracer = setup_tracing(settings, cfg.telemetry)
    stop = asyncio.Event()
    install_signal_handlers(stop)  # before the connection, so an early stop is not a crash
    endpoint: metrics.MetricsServer | None = None
    try:
        endpoint = metrics.serve(settings.metrics_port, settings.metrics_host)
        log.info("metrics on http://%s:%d/metrics", settings.metrics_host, endpoint.port)
        client = await connect(settings.temporal_address, namespace=settings.temporal_namespace)
        log.info(
            "worker running on task queue %s at %s (namespace %s)",
            settings.task_queue,
            settings.temporal_address,
            settings.temporal_namespace,
        )
        await serve(client, settings.task_queue, parts.activities, stop)
        log.info("worker stopped")
    finally:
        if endpoint is not None:
            endpoint.stop()
        await parts.close()
        if tracer is not None:
            tracer.shutdown()  # sends the spans that are still queued
