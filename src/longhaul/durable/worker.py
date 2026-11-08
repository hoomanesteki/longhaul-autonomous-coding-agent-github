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
  local folders only and still recovers the sandbox of a GitHub task. LONGHAUL_GITHUB_API_URL and
  LONGHAUL_GITHUB_WEB_URL point it at a GitHub Enterprise server, or at a FakeGitHub and a folder
  of bare repos in the end-to-end test of LH-308. Unset, they mean api.github.com and github.com.
  LONGHAUL_GITHUB_CLONE_BASE names a folder or server for clones only and wins over the web URL;
  the chaos suite of LH-904 uses it.
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
* The planner (LH-501) gets its own tool list from the same registry: the read and run tools and
  `submit_plan`, and none that writes a file (roles/planner.py). `store_plan` writes the plans
  table and every worker registers it. With the GitHub token the worker also registers
  `post_plan`, which posts the plan as a comment through the same ledger as the other writes.
  Whether a task plans at all is the task's choice (acts.PLAN_MEMO_KEY), not the worker's. The
  workflow keeps its own copy of `loop.planner_max_turns`, so a worker whose config holds
  another number stops before it takes a task. The stuck detector (LH-505) is the same: the
  workflow keeps a copy of the four limits in `stuck`, and `tool_exec` returns the facts it reads.
  The plan gate (LH-502) records who decided a plan through `decide_plan`, which every worker
  registers with `store_plan`. Its wait, and the wait of the pause for a hint (LH-506), are the
  workflow's own copy of `stuck.hint_wait_hours`, so a worker whose config holds another number
  stops before it takes a task for the same reason. `summarize_for_human`, which writes the
  question for a person, is an activity of the compactor and renders its comment from the
  templates folder.
* The end of a task (LH-507) has two activities on the worker. With the GitHub token it registers
  `post_final`, which reads the diff stats from the checkpoint store and posts the final comment
  through the same ledger as the other writes. Every worker registers `clean_repos`, the janitor
  that PollerWorkflow runs after each poll: it removes the bare repos under LONGHAUL_CKPT_DIR of
  tasks that ended more than seven days ago. A worker sweeps the repos on its own disk.
* The coder of a task with a plan (LH-503) gets a list of its own as well: the same tools without
  `finish`, then `update_step`, `submit_for_review` and `ask_human` (roles/coder.py). A coder
  without a plan keeps `finish`. `record_step` writes the events and the `step` checkpoints of
  the step updates, and every worker registers it.
* The verifier (LH-504) has a tool list of its own as well: the read tools, `git_diff` and
  `run_tests`, and `submit_verdict` (roles/verifier.py). It runs on tools that every worker has,
  so it needs no activity of its own. The workflow keeps its own copy of `loop.max_verify_rounds`,
  so a worker whose config holds another number stops before it takes a task.
* The poller (LH-307) reads GitHub with LONGHAUL_GITHUB_TOKEN and starts tasks on this worker's
  own queue. It also carries out the slash commands in the comments of the task issues (LH-308),
  and posts the one reply to a command from someone who may not steer, through the same
  idempotency ledger as the other GitHub writes. Without a token the worker still starts and
  still registers `poll_once`, so a worker for local runs needs none. A poll on it fails with the
  name of the setting, and the Temporal UI shows that. The Schedule that triggers the polls is
  made by `longhaul poller install` and not by the worker.
* Memory follows the `memory` section of the config (LH-604, ADR-0010). With it on, the worker
  builds one PgMemoryStore on its database, with the embedder the config names, and gives it to
  prepare_workspace, which keeps the repo map and the verified commands in it, and to load_memory,
  which reads the notes for the prompt from it. `local` is the real embedder and needs the memory
  extra: a worker without it stops at the start and says `uv sync --extra memory`, and does not
  wait for the first search of a task to find out. `hash` needs nothing and is for tests and CI.
  The vectors of two embedders cannot be compared, so `check_memory` embeds a few stored notes
  again before the worker connects, and refuses a table that another embedder wrote. With it off,
  the worker builds no store: no map and no commands are kept, and the memory block of the
  prompt stays empty. load_memory is registered either way, because the workflow names it.
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
import dataclasses
import logging
import signal
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
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
from longhaul.durable import acts
from longhaul.durable.activities.context import ContextActivities
from longhaul.durable.activities.delivery import DeliveryActivities
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.github import PollerActivities
from longhaul.durable.activities.janitor import JanitorActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.memory import MemoryActivities
from longhaul.durable.activities.outcome import FinalCommentActivities, StoreDiffs
from longhaul.durable.activities.plan import (
    PlanActivities,
    PlanCommentActivities,
    StepActivities,
)
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
from longhaul.memory.embedder import make_embedder
from longhaul.memory.store import PgMemoryStore
from longhaul.providers.base import get_provider
from longhaul.roles import coder, planner, stuck, verifier
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import (
    AppConfig,
    EnvSettings,
    Memory,
    config_path,
    load_config,
    templates_path,
)
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
    memory: PgMemoryStore | None = None

    async def check_memory(self) -> None:
        """Refuse to start on a memory table that another embedder wrote, or on a model that
        cannot load. Nothing to check on a worker without a store.

        The table is read, so a database that cannot be reached stops the worker here, with a
        message, as it would stop its first task.
        """
        if self.memory is None:
            return
        try:
            await self.memory.check_embedder()
        except (SQLAlchemyError, OSError) as exc:
            reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            raise ValueError(f"could not check the memory table of the database: {reason}") from exc

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
    """Where repos are cloned from: github.com, or the base the settings name.

    LONGHAUL_GITHUB_CLONE_BASE (LH-904) names a folder or server for clones only, and wins over
    LONGHAUL_GITHUB_WEB_URL (LH-308), the web address of a GitHub Enterprise server.
    """
    base = settings.github_clone_base or settings.github_web_url
    return github_clone_url if base is None else clone_url_under(base)


def build_memory(cfg: Memory, sessions: async_sessionmaker[AsyncSession]) -> PgMemoryStore | None:
    """The memory store of the worker, or None when the config turns memory off.

    It opens no connection, and the embedder loads no model: a local model is read on the first
    text. A local embedder without its extra raises ValueError here, with the command that
    installs it, so that the worker stops at the start.
    """
    if not cfg.enabled:
        return None
    try:
        embedder = make_embedder(cfg.embedder)
    except ModuleNotFoundError as exc:  # the extra; the command reports a ValueError in one line
        raise ValueError(
            f"memory.embedder is {cfg.embedder} and {exc}. Or set memory.embedder to hash, "
            "or memory.enabled to false"
        ) from exc
    return PgMemoryStore(sessions, embedder)


def _check_stuck_limits(cfg: AppConfig) -> None:
    """Refuse a config whose `stuck` limits the workflow's detector does not use.

    The detector runs in the workflow, which cannot read the config file and keeps its own copy
    of the numbers (stuck.DEFAULT_LIMITS). Like the planner's turn limit, a different number
    would be accepted and then ignored on every task.
    """
    have = stuck.limits_of(cfg.stuck)
    for field in dataclasses.fields(stuck.Limits):
        got, want = getattr(have, field.name), getattr(stuck.DEFAULT_LIMITS, field.name)
        if got != want:
            raise ValueError(
                f"stuck.{field.name} is {got}, but the stuck detector of the workflow uses "
                f"{want} and cannot be told otherwise. Set it to {want}"
            )


def _check_hint_wait(cfg: AppConfig) -> None:
    """Refuse a config whose `stuck.hint_wait_hours` the workflow does not use.

    The plan gate (LH-502) and the pause for a hint (LH-506) run in the workflow, which cannot
    read the config file and waits for acts.HINT_WAIT_HOURS. Like the stuck limits, a different
    number would be accepted and then ignored on every task.
    """
    if cfg.stuck.hint_wait_hours != acts.HINT_WAIT_HOURS:
        raise ValueError(
            f"stuck.hint_wait_hours is {cfg.stuck.hint_wait_hours}, but the workflow waits "
            f"{acts.HINT_WAIT_HOURS} hours for a person and cannot be told otherwise. "
            f"Set it to {acts.HINT_WAIT_HOURS}"
        )


def build_parts(
    settings: EnvSettings, cfg: AppConfig, sandboxes: DockerSandboxManager
) -> WorkerParts:
    """The activities of a worker, wired to the real stores. It opens no connection.

    `sandboxes` is a DockerSandboxManager. It is a parameter because building one connects to
    the Docker daemon, which a test of this wiring does not have.
    """
    if cfg.loop.planner_max_turns != acts.PLANNER_MAX_TURNS:
        # The workflow cannot read the config file and keeps its own copy of the limit. A
        # different number here would be accepted and then ignored on every task.
        raise ValueError(
            f"loop.planner_max_turns is {cfg.loop.planner_max_turns}, but the workflow gives the "
            f"planner {acts.PLANNER_MAX_TURNS} turns and cannot be told otherwise. "
            f"Set it to {acts.PLANNER_MAX_TURNS}"
        )
    _check_stuck_limits(cfg)
    _check_hint_wait(cfg)
    if cfg.loop.max_verify_rounds != acts.MAX_VERIFY_ROUNDS:
        # The same reason as the planner's limit above.
        raise ValueError(
            f"loop.max_verify_rounds is {cfg.loop.max_verify_rounds}, but the workflow ends the "
            f"task after {acts.MAX_VERIFY_ROUNDS} failed reviews and cannot be told otherwise. "
            f"Set it to {acts.MAX_VERIFY_ROUNDS}"
        )
    if settings.database_url is None:
        raise ValueError("LONGHAUL_DATABASE_URL is not set")
    try:
        blobs = S3BlobStore.from_settings(settings)
    except RuntimeError as exc:  # a setting is unset; the command reports it in one line
        raise ValueError(str(exc)) from exc
    store = GitCheckpointStore(blobs, root=settings.ckpt_dir)
    engine = make_engine(settings.database_url.get_secret_value())
    sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
    memory = build_memory(cfg.memory, sessions)
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
        role_tools={
            Role.PLANNER: planner.tool_specs(registry.specs()),
            Role.VERIFIER: verifier.tool_specs(registry.specs()),
        },
        planned_tools=coder.tool_specs(registry.specs()),
    )
    compactor = ContextActivities(
        provider,
        blobs,
        registry.specs(),
        config_for,
        cfg.context,
        ledger=ledger,
        meters=meters,
        templates=templates_path(settings),
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
    # One ledger for every GitHub write, so that a key is held and recorded once.
    effects = EffectLedger(sessions)
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
        memory=memory,
        github=remote,
        blobs=blobs,
    )
    notes = MemoryActivities(memory, blobs)
    events = EventActivities(recorder)
    plans = PlanActivities(recorder)
    steps = StepActivities(recorder)
    janitor = JanitorActivities(store, recorder)
    # The poller reads through the same client that GitHubRemote clones and pushes with, so
    # WorkerParts.close has one connection pool to close.
    poller = PollerActivities(
        github, SqlTaskStore(sessions), cfg, task_queue=settings.task_queue, ledger=effects
    )
    delivery: list[Callable[..., Any]] = []
    if github is not None and remote is not None:
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
            *PlanCommentActivities(github, effects, templates=templates_path(settings)).activities,
            *FinalCommentActivities(
                github, effects, StoreDiffs(store), templates=templates_path(settings)
            ).activities,
        ]
    return WorkerParts(
        activities=[
            *llm.activities,
            *compactor.activities,
            *tools.activities,
            *workspace.activities,
            *notes.activities,
            *events.activities,
            *plans.activities,
            *steps.activities,
            *janitor.activities,
            *poller.activities,
            *delivery,
        ],
        blobs=blobs,
        store=store,
        engine=engine,
        github=github,
        memory=memory,
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
        await parts.check_memory()  # before the port is taken and the queue is polled
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
