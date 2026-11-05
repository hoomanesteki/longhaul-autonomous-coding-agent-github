"""The eval runner: one command for every task set and config (LH-901).

    uv run python eval/run.py --set custom --config full --run-id headline-1

It starts each task on the durable stack (Temporal, the worker, Postgres, MinIO) in local mode,
watches it, reads the patch the agent left, grades the patch, and writes
`eval/results/<run_id>/<task_id>.json`, a `<task_id>.patch` next to it, and a `summary.json`
when the run is over. The numbers in the README come from these files, so every config goes
through this code.

Local mode means the task is a folder on this machine (`local:/abs/path`, as in
`longhaul run`), so nothing is written to GitHub. The plan gate is off, and no hints are given.
A task that pauses needs a person, and nobody is there: it counts as `intervention_needed`, and
the runner ends it when the pause lasts a minute.

The worker is started separately (`uv run longhaul worker`) and must see the same
LONGHAUL_CKPT_DIR as this command, because the patch is read from the checkpoint repo. Its
config decides the model, the context settings and the memory. `--model`, `--memory` and
`--context-mode` are recorded in the result files and `--model` is checked against the config,
but none of them changes the worker. Start it with the config that you mean to measure.

The SWE-bench set (LH-902) and the naive and single-shot configs (LH-903) are accepted as flags
and refused with a message until their stories land.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import re
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

if __package__ in (None, ""):  # started as `python eval/run.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle  # noqa: E402
from temporalio.service import RPCError, RPCStatusCode  # noqa: E402

from eval import custom_tasks, grade_custom, results  # noqa: E402
from longhaul.agent.workspace import DEFAULT_TEST_COMMAND  # noqa: E402
from longhaul.blobs.base import BlobStoreBase  # noqa: E402
from longhaul.checkpoints.git_store import CheckpointError, GitCheckpointStore  # noqa: E402
from longhaul.cli import (  # noqa: E402
    OPEN_STATES,
    POLL_S,
    failure_text,
    make_blobs,
    make_store,
    open_client,
    try_status,
)
from longhaul.contracts import (  # noqa: E402
    Budgets,
    Outcome,
    OutcomeKind,
    Phase,
    RepoSettings,
    TaskSpec,
    TaskStatus,
)
from longhaul.db.models import CostLedgerRow  # noqa: E402
from longhaul.db.session import make_engine  # noqa: E402
from longhaul.durable.client import cancel_task, get_task, start_task  # noqa: E402
from longhaul.durable.scripts import upload_script  # noqa: E402
from longhaul.sandbox.docker_manager import DockerSandboxManager  # noqa: E402
from longhaul.settings import AppConfig, EnvSettings, config_path, load_config  # noqa: E402

ROOT = custom_tasks.ROOT
DEFAULT_RESULTS_DIR = ROOT / "eval" / "results"

# A pause that lasts this long ends the task (plan LH-901). Nobody is there to answer.
PAUSE_TIMEOUT_S = 60.0
# After a cancel the workflow cleans up and ends. A task that has not ended by then is stopped.
CANCEL_GRACE_S = 30.0
# The workflow has its own limit of task_wall_minutes. The runner's is a backstop for a task
# that is stuck somewhere the workflow cannot see, so it comes a little later.
WALL_MARGIN_S = 300.0
# How much of an exception goes into a result file.
FAILURE_CHARS = 500

# Plan section 8.4: the caps that every config and every set shares.
CUSTOM_BUDGETS = Budgets(task_usd=2.0, task_turns=150, task_wall_minutes=120)

PAUSED_PHASES = frozenset({Phase.PAUSED, Phase.AWAITING_PLAN_APPROVAL})
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
WORKFLOW_PREFIX = "lh-eval-"
SCRIPT_SUFFIX = ".yaml"

SETS = ("custom", "swebench")
CONFIGS = ("full", "naive", "single_shot")
SWITCHES = ("on", "off")
CONTEXT_MODES = ("full", "no_compaction", "none")
PROVIDERS = ("anthropic", "openai", "scripted", "replay")


# ---------- options ----------
@dataclass(frozen=True)
class RunOptions:
    task_set: results.TaskSet
    config: results.Config
    run_id: str
    model: str | None = None
    limit: int | None = None
    concurrency: int = 1
    memory: results.Switch = "on"
    context_mode: results.ContextMode | None = None
    provider: str | None = None
    # A script file serves every task. A folder holds one script per task, named <task id>.yaml.
    script: Path | None = None
    results_dir: Path = DEFAULT_RESULTS_DIR
    pause_timeout_s: float = PAUSE_TIMEOUT_S

    @property
    def scripted(self) -> bool:
        """A script plays the answers. The result files say so, because such a run is free."""
        return self.script is not None


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number above 0")
    return value


def positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = 0.0
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number above 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval/run.py",
        description="Run a task set on the durable stack, grade it and write the results.",
    )
    parser.add_argument("--set", dest="task_set", choices=SETS, required=True)
    parser.add_argument("--config", choices=CONFIGS, required=True)
    parser.add_argument("--run-id", required=True, help="names the folder under eval/results")
    parser.add_argument("--model", help="the coder model; it must match the worker's config")
    parser.add_argument("--limit", type=positive_int, help="run only the first N tasks")
    parser.add_argument("--concurrency", type=positive_int, default=1, metavar="K")
    parser.add_argument("--memory", choices=SWITCHES, default="on", help="recorded in the results")
    parser.add_argument("--context-mode", choices=CONTEXT_MODES, help="recorded in the results")
    parser.add_argument("--provider", choices=PROVIDERS)
    parser.add_argument(
        "--script",
        type=Path,
        help="scripted provider: a script file, or a folder of <task id>.yaml",
    )
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--pause-timeout-s", type=positive_float, default=PAUSE_TIMEOUT_S)
    return parser


def parse_args(argv: Sequence[str] | None = None) -> RunOptions:
    """Read the command line. Every mistake ends with a usage error (exit code 2)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not RUN_ID.fullmatch(args.run_id):
        parser.error(
            "--run-id must be 1 to 40 letters, digits, '.', '_' or '-', starting with a letter "
            f"or a digit, not {args.run_id!r}"
        )
    if args.task_set == "swebench":
        parser.error("--set swebench is not available yet: the SWE-bench adapter comes with LH-902")
    if args.config != "full":
        parser.error(f"--config {args.config} is not available yet: the baselines come with LH-903")
    provider = args.provider
    if args.script is not None:
        if provider not in (None, "scripted"):
            parser.error("--script only goes with the scripted provider (--provider scripted)")
        if not args.script.exists():
            parser.error(f"--script: no such file or folder: {args.script}")
        provider = "scripted"
    elif provider == "scripted":
        parser.error("--provider scripted needs --script FILE, or a folder of <task id>.yaml files")
    return RunOptions(
        task_set=args.task_set,
        config=args.config,
        run_id=args.run_id,
        model=args.model,
        limit=args.limit,
        concurrency=args.concurrency,
        memory=args.memory,
        context_mode=args.context_mode,
        provider=provider,
        script=args.script,
        results_dir=args.results_dir,
        pause_timeout_s=args.pause_timeout_s,
    )


# ---------- tasks ----------
def select_tasks(options: RunOptions) -> list[custom_tasks.CustomTask]:
    """The tasks of the set in file order, cut to the first `limit`."""
    if options.task_set != "custom":
        raise ValueError(f"the {options.task_set} set is not available yet")
    tasks = custom_tasks.load_tasks()
    return tasks[: options.limit] if options.limit is not None else tasks


def check_tasks(tasks: Sequence[custom_tasks.CustomTask]) -> None:
    """Refuse to start when a repo folder is not what tasks.yaml says, or would leak the tests.

    The agent works on the folder as it is. If its commit is not the `base_sha` that the grader
    builds the base from, the task would be graded against other files than the agent saw.
    """
    for repo in dict.fromkeys(task.repo for task in tasks):
        folder = custom_tasks.REPOS_DIR / repo
        if not folder.is_dir():
            raise ValueError(f"there is no repo folder {folder}")
        custom_tasks.check_agent_view(folder)
        actual = custom_tasks.compute_base_sha(folder)
        for task in tasks:
            if task.repo == repo and task.base_sha != actual:
                raise ValueError(
                    f"{task.id}: base_sha is out of date, the repo folder is at {actual} "
                    "(python eval/custom_tasks.py prints the current ones)"
                )


def script_for(options: RunOptions, task: custom_tasks.CustomTask) -> Path | None:
    """The script the scripted provider plays for this task, or None for the worker's provider."""
    script = options.script
    if script is None or not script.is_dir():
        return script
    path = script / f"{task.id}{SCRIPT_SUFFIX}"
    if not path.is_file():
        raise ValueError(f"{script} has no script for {task.id}: expected {task.id}{SCRIPT_SUFFIX}")
    return path


def workflow_id_for(run_id: str, task: custom_tasks.CustomTask) -> str:
    """The id of the workflow. It names the run, so two runs of one task never meet."""
    return f"{WORKFLOW_PREFIX}{run_id}-{task.id}"


def build_spec(task: custom_tasks.CustomTask, workflow_id: str) -> TaskSpec:
    """The task as the workflow gets it: a local folder, no issue number, no plan gate."""
    repo = f"local:{custom_tasks.REPOS_DIR / task.repo}"
    return TaskSpec(
        task_id=workflow_id,
        repo=repo,
        issue_number=None,
        title=task.issue_title,
        body=task.issue_body,
        settings=RepoSettings(
            repo=repo,
            plan_approval_required=False,
            test_command=DEFAULT_TEST_COMMAND,
            budgets=CUSTOM_BUDGETS,
        ),
    )


def resolve_model(options: RunOptions, cfg: AppConfig) -> str:
    """The model the result files name. The worker picks it from its config, so it is checked."""
    coder = cfg.models.coder
    if options.model is not None and options.model != coder:
        raise ValueError(
            f"--model {options.model} is not the coder model of the config ({coder}). The worker "
            f"picks the model from its config (LONGHAUL_CONFIG), so start it with a config "
            f"that names {options.model} as the coder"
        )
    return coder


def claim_run_dir(results_dir: Path, run_id: str) -> Path:
    """A new folder for a run. A run id whose folder exists is not used a second time.

    An empty folder counts too. A run that was interrupted leaves one, and its tasks have
    started workflows under this run id, which Temporal does not start a second time.
    """
    folder = results_dir / run_id
    try:
        folder.mkdir(parents=True)
    except FileExistsError:
        raise ValueError(
            f"run id {run_id} was used already: {folder} exists. Use another --run-id"
        ) from None
    return folder


async def check_workflow_ids_free(client: Client, workflow_ids: Sequence[str]) -> None:
    """Stop when a workflow of this run id exists, whether it is open or closed.

    The results folder is not the only record of a run: it may have been moved or deleted.
    Temporal refuses to start an id a second time, so the run would fail task by task.
    """
    for workflow_id in workflow_ids:
        try:
            await get_task(client, workflow_id).describe()
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                continue
            raise ConnectionError(f"could not ask Temporal about {workflow_id}: {exc}") from exc
        raise ValueError(
            f"the workflow {workflow_id} exists already, so this run id was used before. "
            "Use another --run-id"
        )


def git_sha() -> str:
    """The commit of this checkout, so a result says which code produced it."""
    try:
        done = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return done.stdout.strip()


# ---------- watching a task ----------
class WatchedTask(Protocol):
    async def is_open(self) -> bool: ...

    async def status(self) -> TaskStatus | None: ...

    async def cancel(self, reason: str) -> None: ...

    async def terminate(self, reason: str) -> None: ...


@dataclass
class Watched:
    """What the watch saw."""

    intervention_needed: bool = False
    stopped_by: str | None = None
    status: TaskStatus | None = None


async def watch(
    task: WatchedTask,
    *,
    pause_timeout_s: float,
    wall_limit_s: float,
    poll_s: float = POLL_S,
    cancel_grace_s: float = CANCEL_GRACE_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Watched:
    """Follow a task until it ends, and end it when it has to be ended.

    Any pause marks the task as needing a person. A pause that lasts `pause_timeout_s` ends the
    task, and so does running past `wall_limit_s`. The end is a cancel, which lets the workflow
    clean up. When the task is still open `cancel_grace_s` later, it is terminated.
    """
    watched = Watched()
    started = clock()
    paused_since: float | None = None
    while await task.is_open():
        now = clock()
        status = await task.status()
        if status is not None:  # no answer means nobody knows, and that is not a pause
            watched.status = status
            if status.phase in PAUSED_PHASES:
                watched.intervention_needed = True
                paused_since = now if paused_since is None else paused_since
            else:
                paused_since = None
        reason = None
        if now - started >= wall_limit_s:
            reason = f"the time limit of {wall_limit_s:.0f} s passed"
        elif paused_since is not None and now - paused_since >= pause_timeout_s:
            why = (watched.status.paused_reason if watched.status else None) or "no reason given"
            reason = f"the task paused ({why}) and nobody answered in {pause_timeout_s:.0f} s"
        if reason is not None:
            watched.stopped_by = reason
            await stop(
                task, reason, grace_s=cancel_grace_s, poll_s=poll_s, clock=clock, sleep=sleep
            )
            break
        await sleep(poll_s)
    return watched


async def stop(
    task: WatchedTask,
    reason: str,
    *,
    grace_s: float,
    poll_s: float,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> None:
    await task.cancel(reason)
    deadline = clock() + grace_s
    while await task.is_open():
        if clock() >= deadline:
            await task.terminate(reason)
            return
        await sleep(poll_s)


class TemporalTask:
    """A task on the server, seen through its workflow handle."""

    def __init__(self, client: Client, workflow_id: str) -> None:
        self._client = client
        self._id = workflow_id
        self.handle: WorkflowHandle[Any, Outcome] = get_task(client, workflow_id)

    async def is_open(self) -> bool:
        return (await self.handle.describe()).status in OPEN_STATES

    async def status(self) -> TaskStatus | None:
        return await try_status(self.handle)

    async def cancel(self, reason: str) -> None:
        await cancel_task(self._client, self._id, by="eval-runner", reason=reason)

    async def terminate(self, reason: str) -> None:
        await self.handle.terminate(reason=reason)


# ---------- one task ----------
@dataclass
class RunContext:
    options: RunOptions
    settings: EnvSettings
    client: Client
    blobs: BlobStoreBase
    store: GitCheckpointStore
    sessions: async_sessionmaker[AsyncSession]
    sandboxes: DockerSandboxManager
    grader: grade_custom.Grader
    model: str
    commit: str
    folder: Path
    wall_limit_s: float = (CUSTOM_BUDGETS.task_wall_minutes * 60) + WALL_MARGIN_S


async def ledger_totals(
    sessions: async_sessionmaker[AsyncSession], workflow_id: str
) -> tuple[float, results.Tokens]:
    """What a task cost and used, summed over every row of the ledger, retries included."""
    columns = (
        func.coalesce(func.sum(CostLedgerRow.usd), 0),
        func.coalesce(func.sum(CostLedgerRow.input_tokens), 0),
        func.coalesce(func.sum(CostLedgerRow.output_tokens), 0),
        func.coalesce(func.sum(CostLedgerRow.cache_read_tokens), 0),
        func.coalesce(func.sum(CostLedgerRow.cache_write_tokens), 0),
    )
    async with sessions() as session:
        usd, inputs, outputs, reads, writes = (
            await session.execute(select(*columns).where(CostLedgerRow.task_id == workflow_id))
        ).one()
    tokens = results.Tokens(
        input=int(inputs), output=int(outputs), cache_read=int(reads), cache_write=int(writes)
    )
    return round(float(usd), 6), tokens


async def read_patch(
    store: GitCheckpointStore, workflow_id: str, status: TaskStatus | None
) -> tuple[str, str | None]:
    """The agent's patch from the checkpoint repo, and the reason when there is none to read."""
    if status is None or status.last_checkpoint is None:
        return "", "the workflow did not say where it ended"
    try:
        return await store.diff(workflow_id, status.last_checkpoint), None
    except (CheckpointError, ValueError) as exc:
        return "", f"could not read the patch: {exc}"


async def read_outcome(handle: WorkflowHandle[Any, Outcome], stopped_by: str | None) -> Outcome:
    try:
        return await handle.result()
    except WorkflowFailureError as exc:
        # A task that the runner had to terminate fails with a termination. It was ended on
        # purpose, which is a cancel, and not an error of the agent.
        kind = OutcomeKind.CANCELLED if stopped_by else OutcomeKind.ERROR
        return Outcome(kind=kind, summary=failure_text(exc))


async def grade(
    ctx: RunContext, task: custom_tasks.CustomTask, outcome: Outcome, patch: str, workflow_id: str
) -> tuple[bool | None, str]:
    """(resolved, one line). A task counts as resolved when the agent finished and the grader
    passes the patch. One that ended any other way is not graded, because nothing was delivered.
    """
    if outcome.kind is not OutcomeKind.PR_OPENED:
        return False, f"not graded: the task ended as {outcome.kind.value}"
    try:
        graded = await ctx.grader.grade(task, patch, name=f"{workflow_id}-grade")
    except Exception as exc:
        # Docker or the network failed. The agent's work is done and recorded, so the task is
        # reported as not graded, which the summary counts, and the run goes on.
        return None, f"the grader failed: {exc}"
    return graded.resolved, graded.reason


async def abandon(ctx: RunContext, workflow_id: str) -> None:
    """Stop a task whose run was interrupted, and remove what it left on this machine."""
    with contextlib.suppress(Exception):
        await get_task(ctx.client, workflow_id).terminate(reason="the eval runner stopped")
    await clean_up(ctx, workflow_id)


async def clean_up(ctx: RunContext, workflow_id: str) -> None:
    """Remove the sandbox and the checkpoint repo of a task. A finished workflow leaves both."""
    for remove in (ctx.sandboxes.cleanup_task, ctx.store.cleanup_task):
        with contextlib.suppress(Exception):
            await remove(workflow_id)


def identity(ctx: RunContext, task: custom_tasks.CustomTask) -> dict[str, Any]:
    """The fields that every result of this run shares, whether the task finished or not."""
    options = ctx.options
    return {
        "run_id": options.run_id,
        "task_id": task.id,
        "workflow_id": workflow_id_for(options.run_id, task),
        "task_set": options.task_set,
        "config": options.config,
        "model": ctx.model,
        "git_sha": ctx.commit,
        "provider": options.provider,
        "scripted": options.scripted,
        "memory": options.memory,
        "context_mode": options.context_mode,
    }


async def run_one(ctx: RunContext, task: custom_tasks.CustomTask) -> results.TaskResult:
    """Run one task and write its result file.

    When the runner itself fails on the task, with a lost connection or a database error, the
    task ends with an error result and the other tasks go on. A stop of the whole run (Ctrl-C)
    is not a failure of one task and passes through.
    """
    started_at = datetime.now(UTC)
    try:
        return await execute_task(ctx, task, started_at)
    except Exception as exc:
        return await record_failure(ctx, task, started_at, exc)


async def record_failure(
    ctx: RunContext, task: custom_tasks.CustomTask, started_at: datetime, exc: Exception
) -> results.TaskResult:
    """Write the result of a task that the runner could not run to the end."""
    ended_at = datetime.now(UTC)
    workflow_id = workflow_id_for(ctx.options.run_id, task)
    try:
        cost_usd, tokens = await ledger_totals(ctx.sessions, workflow_id)
    except Exception:  # the ledger may be what failed; the task still gets its result
        cost_usd, tokens = 0.0, results.Tokens()
    message = f"the runner failed: {type(exc).__name__}: {exc}"[:FAILURE_CHARS]
    result = results.TaskResult(
        **identity(ctx, task),
        outcome=OutcomeKind.ERROR.value,
        outcome_summary=message,
        resolved=None,
        grading=f"not graded: {message}",
        cost_usd=cost_usd,
        tokens=tokens,
        cache_ratio=results.cache_ratio(tokens),
        started_at=started_at,
        ended_at=ended_at,
        wall_time_s=(ended_at - started_at).total_seconds(),
        turns=0,
    )
    results.write_result(ctx.folder, result)
    print(describe(result), flush=True)
    return result


async def execute_task(
    ctx: RunContext, task: custom_tasks.CustomTask, started_at: datetime
) -> results.TaskResult:
    options = ctx.options
    workflow_id = workflow_id_for(options.run_id, task)
    script = script_for(options, task)
    script_ref = await upload_script(ctx.blobs, workflow_id, script) if script else None
    try:
        await start_task(
            ctx.client,
            build_spec(task, workflow_id),
            task_queue=ctx.settings.task_queue,
            script=script_ref,
        )
        live = TemporalTask(ctx.client, workflow_id)
        watched = await watch(
            live, pause_timeout_s=options.pause_timeout_s, wall_limit_s=ctx.wall_limit_s
        )
        outcome = await read_outcome(live.handle, watched.stopped_by)
        ended_at = datetime.now(UTC)
        final = await try_status(live.handle) or watched.status
        patch, unreadable = await read_patch(ctx.store, workflow_id, final)
        resolved, grading = await grade(ctx, task, outcome, patch, workflow_id)
        cost_usd, tokens = await ledger_totals(ctx.sessions, workflow_id)
    except BaseException:
        await abandon(ctx, workflow_id)
        raise
    await clean_up(ctx, workflow_id)
    if unreadable and outcome.kind is OutcomeKind.PR_OPENED:
        grading = f"{grading} ({unreadable})"
    (ctx.folder / f"{results.check_task_id(task.id)}.patch").write_text(patch, encoding="utf-8")
    result = results.TaskResult(
        **identity(ctx, task),
        outcome=outcome.kind.value,
        outcome_summary=outcome.summary,
        resolved=resolved,
        grading=grading,
        cost_usd=cost_usd,
        tokens=tokens,
        cache_ratio=results.cache_ratio(tokens),
        started_at=started_at,
        ended_at=ended_at,
        wall_time_s=(ended_at - started_at).total_seconds(),
        turns=final.turn_index if final else 0,
        intervention_needed=watched.intervention_needed,
        stopped_by_runner=watched.stopped_by,
    )
    results.write_result(ctx.folder, result)
    print(describe(result), flush=True)
    return result


def describe(result: results.TaskResult) -> str:
    verdict = {True: "resolved", False: "not resolved", None: "not graded"}[result.resolved]
    return (
        f"{result.task_id}: {result.outcome}, {verdict}, ${result.cost_usd:.4f}, "
        f"{result.turns} turns, {result.wall_time_s:.0f} s ({result.grading})"
    )


# ---------- the run ----------
async def run_tasks(
    ctx: RunContext, tasks: Sequence[custom_tasks.CustomTask]
) -> list[results.TaskResult]:
    """Run the tasks, `concurrency` at a time, and return their results in task order.

    run_one turns a failure of one task into its result, so one bad task never cancels the
    others that are still running.
    """
    gate = asyncio.Semaphore(ctx.options.concurrency)

    async def limited(task: custom_tasks.CustomTask) -> results.TaskResult:
        async with gate:
            return await run_one(ctx, task)

    async with asyncio.TaskGroup() as group:
        running = [group.create_task(limited(task)) for task in tasks]
    return [job.result() for job in running]


async def run_eval(options: RunOptions, settings: EnvSettings) -> results.Summary:
    """Run the tasks, write the result files and the summary, and return the summary.

    Everything that can be checked without starting a task is checked first, so a mistake costs
    nothing: the model, the repos, the scripts, the settings of the stack and the run id. The
    results folder is made last, so a run that stops before its first task leaves no trace.
    """
    cfg = load_config(config_path(settings))
    model = resolve_model(options, cfg)
    tasks = select_tasks(options)
    await asyncio.to_thread(check_tasks, tasks)
    for task in tasks:
        script_for(options, task)
    if settings.database_url is None:
        raise ValueError("LONGHAUL_DATABASE_URL is not set: the cost comes from the cost ledger")

    blobs = make_blobs(settings)
    client = await open_client(settings)
    await check_workflow_ids_free(client, [workflow_id_for(options.run_id, t) for t in tasks])
    folder = claim_run_dir(options.results_dir, options.run_id)

    engine = make_engine(settings.database_url.get_secret_value())
    sandboxes = DockerSandboxManager()
    started_at = datetime.now(UTC)
    try:
        ctx = RunContext(
            options=options,
            settings=settings,
            client=client,
            blobs=blobs,
            store=make_store(blobs, settings),
            sessions=async_sessionmaker[AsyncSession](engine, expire_on_commit=False),
            sandboxes=sandboxes,
            grader=grade_custom.Grader(
                sandboxes, image=cfg.sandbox.image, limits=cfg.sandbox.limits
            ),
            model=model,
            commit=git_sha(),
            folder=folder,
        )
        finished = await run_tasks(ctx, tasks)
    finally:
        await engine.dispose()
    summary = results.summarize(finished, started_at=started_at, ended_at=datetime.now(UTC))
    results.write_summary(folder, summary)
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    unapplied_provider = options.provider not in (None, "scripted")
    if options.memory != "on" or options.context_mode is not None or unapplied_provider:
        print(
            "note: --memory, --context-mode and --provider are recorded in the results. The "
            "worker takes them from its own config, so start it with the settings you mean to "
            "measure.",
            file=sys.stderr,
        )
    try:
        summary = asyncio.run(run_eval(options, EnvSettings()))
    except (ValueError, ConnectionError, custom_tasks.HiddenTestsVisibleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"run {summary.run_id}: {summary.resolved} of {summary.attempted} resolved, "
        f"${summary.cost_usd:.4f}, {summary.intervention_needed} needed a person "
        f"({options.results_dir / summary.run_id})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
