"""The worker process: `longhaul worker`.

A worker polls one Temporal task queue and runs the activities of TaskWorkflow. It is the only
process that holds model keys, talks to Docker, and writes the database. The workflow itself runs
in the worker too, under the sandbox settings of durable/workflow_sandbox.py.

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
* Every activity logs when it starts, ends or fails (durable/activity_log.py). After a SIGKILL
  that is all a worker leaves behind, and it says where the kill landed.
* The tools are the default set. `run_tests` has one test command for the whole worker, because
  the workflow does not pass the task's settings to `tool_exec`.

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
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import ToolActivities
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.durable.activity_log import ActivityLogInterceptor
from longhaul.durable.client import connect
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.base import get_provider
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import AppConfig, EnvSettings, config_path, load_config
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

    async def close(self) -> None:
        """Close the database pool. The other clients hold no connection that needs closing."""
        await self.engine.dispose()


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
    registry = default_registry(
        test_command=DEFAULT_TEST_COMMAND, max_timeout_s=cfg.sandbox.command_timeout_s
    )

    def config_for(role: Role) -> LoopConfig:
        return LoopConfig.from_config(cfg, role)

    llm = LlmActivities(
        build_provider(settings), blobs, recorder, registry.specs(), config_for, ledger=ledger
    )
    tools = ToolActivities(
        registry,
        sandboxes,
        blobs,
        recorder,
        checkpoints=store,
        tool_output_max_tokens=cfg.context.tool_output_max_tokens,
    )
    workspace = WorkspaceActivities(
        sandboxes, store, recorder, image=cfg.sandbox.image, limits=cfg.sandbox.limits
    )
    events = EventActivities(recorder)
    return WorkerParts(
        activities=[
            *llm.activities,
            *tools.activities,
            *workspace.activities,
            *events.activities,
        ],
        blobs=blobs,
        store=store,
        engine=engine,
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
        workflows=[TaskWorkflow],
        activities=activities,
        workflow_runner=workflow_runner(),
        graceful_shutdown_timeout=grace,
        interceptors=[ActivityLogInterceptor()],
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
    try:
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
        await parts.close()
        if tracer is not None:
            tracer.shutdown()  # sends the spans that are still queued
