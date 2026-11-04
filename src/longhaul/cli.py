"""The `longhaul` command line.

Five commands and two groups, `memory` and `poller`. `run-local` copies a folder into a Docker
sandbox, lets the agent work on the issue in a text file, and prints what it did. No Temporal
and no database are involved, so it is the quickest way to watch the agent work.

The other four use the durable stack that `make up` starts. `worker` runs the process that does
the work (durable/worker.py). `run` starts a task for a folder on that stack and follows it
until it ends. `status` and `cancel` ask about a task by its id, or stop it. They are separate
commands because a task outlives `run`: Ctrl-C ends the command and the task goes on.

`poller` is the group that manages the Temporal Schedule which finds labeled GitHub issues and
starts a task for each one (LH-307, durable/schedule.py). `poller install` creates the Schedule
or brings it to the interval of `poller.interval_seconds`, and running it again changes nothing.
`pause`, `resume` and `trigger` act on it. They need Temporal and a config, and no worker: the
worker is what runs the polls.

What `run-local` prints, on stdout in this order: a header with the task id, the repo, the
provider and the model; one line per turn; one line for how the run ended; and the final
`git_diff`. Progress notes, warnings and errors go to stderr, so the output can be piped on.
`run` prints a header, a status line each time the status of the task changes, the outcome and
the final diff, in the same shape.

Exit codes. 0: the agent called `finish`. 2: the run failed or ended in error (turn limit, no
tool calls, a refusal, a failing setup, a provider error, a missing Docker or image), or it
was interrupted. Bad options exit 2 as well, which is what Typer does for usage errors. An
unexpected exception is a bug in longhaul and exits 1 with its traceback.

The body of an issue file is cut to the limit of a GitHub issue, by the function that intake.py
cuts a GitHub body with. The command says so on stderr, and the model sees a note after the kept
text.

`run-local` removes the sandbox at the end, together with its volume, whatever happened: a
finish, an error, a bug, Ctrl-C, SIGTERM or SIGHUP. Only SIGKILL and a crash of the machine
leave one behind. Its container and volume carry the task id, which the header line prints.
`run` removes the sandbox of a task that has ended and leaves a task that is still running alone.

`memory` is a group of commands on the notes the agent keeps about a repo (ADR-0010). They read
the database of LONGHAUL_DATABASE_URL: `memory list --repo REPO` shows the items, and
`memory approve ID` and `memory reject ID` decide on a candidate. The person who decides is
recorded as the approver, from `--by` or else the name of the user of the machine. A missing
item, a missing database setting and a database that cannot be reached exit 2 with one line.
"""

import asyncio
import getpass
import re
import signal
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from docker.errors import DockerException, ImageNotFound
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowFailureError,
    WorkflowHandle,
    WorkflowQueryFailedError,
)
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from longhaul.agent.loop import CallEvent, LoopConfig, TurnEvent, run_loop
from longhaul.agent.workspace import (
    DEFAULT_TEST_COMMAND,
    WorkspaceError,
    WorkspaceManager,
    default_setup_command,
    prepare_local_workspace,
)
from longhaul.blobs.base import BlobError, BlobStoreBase
from longhaul.blobs.s3 import S3BlobStore
from longhaul.checkpoints.git_store import CheckpointError, GitCheckpointStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMProvider,
    Outcome,
    OutcomeKind,
    ProviderError,
    RepoSettings,
    Role,
    Sandbox,
    TaskSpec,
    TaskStatus,
    ToolCall,
    ToolContext,
)
from longhaul.db.session import database_url, make_engine
from longhaul.durable import acts, schedule
from longhaul.durable.client import cancel_task, connect, get_task, read_status, start_task
from longhaul.durable.scripts import upload_script
from longhaul.github.intake import FILE_CUT_NOTE, MAX_BODY_BYTES, clip_text
from longhaul.memory.embedder import LocalEmbedder
from longhaul.memory.store import KINDS, STATUSES, MemoryEntry, PgMemoryStore
from longhaul.providers.base import get_provider
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import AppConfig, EnvSettings, config_path, load_config
from longhaul.tools.git import NO_CHANGES
from longhaul.tools.registry import ToolRegistry, default_registry

DIFF_HEADING = "final diff against base:"
EXIT_ERROR = 2

# How often `run` asks the workflow for its status, and how long it waits for an answer. A query
# is answered by a worker, so with no worker running it would otherwise wait for the server's own
# limit, and `run` would show nothing for that long.
POLL_S = 2.0
STATUS_TIMEOUT_S = 5.0

# A workflow in one of these states can still change. CONTINUED_AS_NEW is the end of a run and
# the start of the next one in the same workflow id, so the task is not over yet.
OPEN_STATES = frozenset({WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.CONTINUED_AS_NEW})

# Failures of the machine, the config or the run, which the command reports in one line. Any
# other exception is a bug and keeps its traceback.
RUN_ERRORS = (
    WorkspaceError,
    ProviderError,
    DockerException,
    OSError,
    ValueError,
    NotImplementedError,
    yaml.YAMLError,
)
# The same for the durable commands, which also talk to Temporal and the stores.
DURABLE_ERRORS = (*RUN_ERRORS, RPCError, WorkflowAlreadyStartedError, BlobError, CheckpointError)

# Failures of the database or of the request to the memory commands, which they report in one
# line: a missing item, a blank name, a database that is not configured or cannot be reached.
MEMORY_ERRORS = (LookupError, ValueError, OSError, SQLAlchemyError)
# The approver when `--by` is not given and the machine has no user name to offer.
FALLBACK_APPROVER = "cli"
# The longest content that `memory list` prints on its line.
LIST_CONTENT_CHARS = 100

app = typer.Typer(
    name="longhaul",
    add_completion=False,
    no_args_is_help=True,
    # A traceback with locals could print the API key held in the settings.
    pretty_exceptions_enable=False,
)
memory_app = typer.Typer(
    name="memory",
    help="Look at what the agent remembers about a repo, and decide on what it proposes.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(memory_app, name="memory")


poller_app = typer.Typer(
    name="poller",
    help="Manage the Temporal Schedule that starts a task for each labeled GitHub issue.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(poller_app, name="poller")


class ProviderName(StrEnum):
    SCRIPTED = "scripted"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


@dataclass(frozen=True)
class RunOptions:
    path: Path
    issue_file: Path
    provider: ProviderName | None
    script: Path | None
    model: str | None
    test_command: str
    max_turns: int | None


@dataclass(frozen=True)
class Run:
    """What one run needs once its inputs are read and checked."""

    options: RunOptions
    cfg: AppConfig
    loop_cfg: LoopConfig
    provider: LLMProvider
    title: str
    body: str
    task_id: str


@app.callback()
def main() -> None:
    """Longhaul, a background coding agent."""


@app.command("run-local")
def run_local(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            help="Folder with the repo. It is copied into the sandbox and never changed.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ],
    issue_file: Annotated[
        Path,
        typer.Option(
            "--issue-file",
            help="Text or Markdown file with the task. Its first line is the title.",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
    provider: Annotated[
        ProviderName | None,
        typer.Option(
            "--provider",
            help="Who answers: a script, Anthropic or an OpenAI-compatible endpoint. "
            "Default: LONGHAUL_PROVIDER.",
        ),
    ] = None,
    script: Annotated[
        Path | None,
        typer.Option(
            "--script",
            help="YAML script for the scripted provider. Default: LONGHAUL_SCRIPT.",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Model id. Default: models.coder in the config."),
    ] = None,
    test_command: Annotated[
        str,
        typer.Option("--test-command", help="How the agent runs the repo's tests."),
    ] = DEFAULT_TEST_COMMAND,
    max_turns: Annotated[
        int | None,
        typer.Option(
            "--max-turns",
            min=1,
            help=f"Stop after this many turns. Default: {Budgets().task_turns}.",
        ),
    ] = None,
) -> None:
    """Let the agent work on an issue, in a sandbox copy of a local repo.

    Prints each turn and the final diff. Exits 0 when the agent finishes, 2 when the run fails.
    """
    options = RunOptions(path, issue_file, provider, script, model, test_command, max_turns)
    try:
        code = asyncio.run(execute(options))
    except RUN_ERRORS as exc:
        typer.echo(f"error: {describe(exc)}", err=True)
        raise typer.Exit(EXIT_ERROR) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        typer.echo("interrupted", err=True)
        raise typer.Exit(EXIT_ERROR) from None
    if code:
        raise typer.Exit(code)


@app.command("worker")
def worker_command() -> None:
    """Run a worker: the process that does the work of the tasks.

    It polls the task queue on Temporal until it gets Ctrl-C or SIGTERM, then waits a short time
    for the activities that are running and stops. It needs the stack from `make up`, the sandbox
    image from `make sandbox-image`, and the LONGHAUL_* settings of .env.example.
    """
    # Imported here: the worker pulls in SQLAlchemy, boto3 and every activity, which the other
    # commands do not need.
    from longhaul.durable import worker
    from longhaul.telemetry.logging import setup_logging

    # JSON lines with the task context, and secrets redacted (LH-706).
    setup_logging()
    try:
        asyncio.run(worker.main())
    except DURABLE_ERRORS as exc:
        typer.echo(f"error: {describe(exc)}", err=True)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("run")
def run(
    path: Annotated[
        Path,
        typer.Option(
            "--path",
            help="Folder with the repo. The worker copies it and never changes it. It has to "
            "be on the machine of the worker.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ],
    issue_file: Annotated[
        Path,
        typer.Option(
            "--issue-file",
            help="Text or Markdown file with the task. Its first line is the title.",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
    provider: Annotated[
        ProviderName | None,
        typer.Option(
            "--provider",
            help="With `scripted` the task brings its script. Any other value changes nothing: "
            "the worker answers with its own LONGHAUL_PROVIDER. Default: LONGHAUL_PROVIDER.",
        ),
    ] = None,
    script: Annotated[
        Path | None,
        typer.Option(
            "--script",
            help="YAML script for the scripted provider. It is sent to the worker with the "
            "task. Default: LONGHAUL_SCRIPT.",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
) -> None:
    """Start a task on the durable stack and follow it until it ends.

    Prints the status each time it changes, then the outcome and the final diff. Exits 0 when
    the agent finishes and 2 when the run fails. Ctrl-C stops the command and not the task.
    """
    options = DurableOptions(path, issue_file, provider, script)
    try:
        code = asyncio.run(submit(options))
    except DURABLE_ERRORS as exc:
        typer.echo(f"error: {describe(exc)}", err=True)
        raise typer.Exit(EXIT_ERROR) from None
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise typer.Exit(EXIT_ERROR) from None  # submit() has said what to do next
    if code:
        raise typer.Exit(code)


@app.command("status")
def status(
    task_id: Annotated[str, typer.Argument(help="The task id that `longhaul run` printed.")],
) -> None:
    """Show where a task stands: its state, its status, and its outcome once it has ended."""
    try:
        asyncio.run(show_status(task_id))
    except DURABLE_ERRORS as exc:
        typer.echo(f"error: {describe(exc, task_id)}", err=True)
        raise typer.Exit(EXIT_ERROR) from None


@app.command("cancel")
def cancel(
    task_id: Annotated[str, typer.Argument(help="The task id that `longhaul run` printed.")],
    reason: Annotated[
        str, typer.Option("--reason", help="Why. It goes into the outcome of the task.")
    ] = "",
) -> None:
    """Stop a running task. The workflow removes its sandbox and ends as cancelled.

    The command returns when the signal is sent. Follow the end with `longhaul status`.
    """
    try:
        asyncio.run(send_cancel(task_id, reason))
    except DURABLE_ERRORS as exc:
        typer.echo(f"error: {describe(exc, task_id)}", err=True)
        raise typer.Exit(EXIT_ERROR) from None
    typer.echo(f"cancel sent to task {task_id}")


# ---------- memory ----------
@asynccontextmanager
async def open_memory() -> AsyncIterator[PgMemoryStore]:
    """The memory store on the database of LONGHAUL_DATABASE_URL, closed at the end.

    A function of its own so that unit tests can replace it. These commands never embed a text,
    so the local model is not loaded: LocalEmbedder reads its model when it is first used.
    """
    try:
        url = database_url()
    except RuntimeError as exc:
        raise ValueError(str(exc)) from exc
    # One short loop per command: a pooled connection would wait for a loop that is gone.
    engine = make_engine(url, pooled=False)
    try:
        sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
        yield PgMemoryStore(sessions, LocalEmbedder())
    finally:
        await engine.dispose()


def with_memory[T](job: Callable[[PgMemoryStore], Awaitable[T]]) -> T:
    """Run `job` on the memory store. A failure of the request or the database exits with 2."""

    async def main() -> T:
        async with open_memory() as store:
            return await job(store)

    try:
        return asyncio.run(main())
    except MEMORY_ERRORS as exc:
        typer.echo(f"error: {describe(exc)}", err=True)
        raise typer.Exit(EXIT_ERROR) from None


def one_of(allowed: tuple[str, ...]) -> Callable[[str | None], str | None]:
    """An option check for a value that must be one of `allowed`."""

    def check(value: str | None) -> str | None:
        if value is not None and value not in allowed:
            raise typer.BadParameter(f"{value!r} is not one of {', '.join(allowed)}")
        return value

    return check


def default_approver() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no user name in the environment or in the password file
        return FALLBACK_APPROVER


def one_line(text: str, limit: int = LIST_CONTENT_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def format_entry(entry: MemoryEntry) -> str:
    return (
        f"{entry.id:<36}  {entry.status:<9}  {entry.kind:<10}  {entry.source:<14}  "
        f"{entry.uses:>4}  {one_line(entry.content)}"
    )


LIST_HEADER = f"{'ID':<36}  {'STATUS':<9}  {'KIND':<10}  {'SOURCE':<14}  {'USES':>4}  CONTENT"


@memory_app.command("list")
def memory_list(
    repo: Annotated[str, typer.Option("--repo", help="The repo, as owner/name.")],
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help=f"Only items with this status: {', '.join(STATUSES)}.",
            callback=one_of(STATUSES),
        ),
    ] = None,
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            help=f"Only items of this kind: {', '.join(KINDS)}.",
            callback=one_of(KINDS),
        ),
    ] = None,
) -> None:
    """List the items the agent remembers about a repo, oldest first.

    Only approved items reach a prompt. Candidates wait here for `memory approve`.
    """
    entries = with_memory(lambda store: store.list_items(repo, status=status, kind=kind))
    if not entries:
        typer.echo(f"no memory items for {repo}", err=True)
        return
    typer.echo(LIST_HEADER)
    for entry in entries:
        typer.echo(format_entry(entry))


ItemId = Annotated[str, typer.Argument(metavar="ID", help="The id that `memory list` shows.")]
Approver = Annotated[
    str | None,
    typer.Option("--by", help="Who decides. Default: the name of the user of this machine."),
]


@memory_app.command("approve")
def memory_approve(item_id: ItemId, by: Approver = None) -> None:
    """Approve an item, so that it can reach the prompt of later tasks."""
    who = by if by is not None else default_approver()
    with_memory(lambda store: store.promote(item_id, who))
    typer.echo(f"approved {item_id} (by {who})")


@memory_app.command("reject")
def memory_reject(item_id: ItemId, by: Approver = None) -> None:
    """Reject an item. It stays in the table and never reaches a prompt."""
    who = by if by is not None else default_approver()
    with_memory(lambda store: store.reject(item_id, who))
    typer.echo(f"rejected {item_id} (by {who})")


# ---------- the poller ----------
@poller_app.command("install")
def poller_install() -> None:
    """Create the poller Schedule, or bring it to the interval of the config.

    The interval is `poller.interval_seconds` and the poll runs on the task queue of
    LONGHAUL_TASK_QUEUE, which is the queue of the worker. Run it again after a change of
    either: it updates the Schedule when they differ and does nothing when they do not. A
    paused Schedule stays paused.
    """
    run_poller_command(install_poller_schedule())


@poller_app.command("pause")
def poller_pause(
    note: Annotated[
        str, typer.Option("--note", help="Why. It shows next to the Schedule in the Temporal UI.")
    ] = "",
) -> None:
    """Stop the polls. A poll that is running finishes. Start them again with `resume`."""
    run_poller_command(pause_poller_schedule(note))


@poller_app.command("resume")
def poller_resume() -> None:
    """Start the polls again after a `pause`."""
    run_poller_command(resume_poller_schedule())


@poller_app.command("trigger")
def poller_trigger() -> None:
    """Poll GitHub now, without waiting for the next tick. It works on a paused Schedule too."""
    run_poller_command(trigger_poller_schedule())


# ---------- the run ----------
def make_manager() -> WorkspaceManager:
    """The sandbox manager. A function of its own so that unit tests can replace it."""
    return DockerSandboxManager()


def build_provider(
    name: ProviderName | None, settings: EnvSettings, script: Path | None
) -> LLMProvider:
    chosen = settings if name is None else settings.model_copy(update={"provider": name.value})
    return get_provider(chosen, script_path=script)


async def execute(options: RunOptions) -> int:
    """Read and check every input, then run. Nothing touches Docker before this is done."""
    settings = EnvSettings()
    cfg = load_config(config_path(settings))
    title, body = read_issue(options.issue_file)
    provider = build_provider(options.provider, settings, options.script)
    run = Run(
        options=options,
        cfg=cfg,
        loop_cfg=loop_config(cfg, options),
        provider=provider,
        title=title,
        body=body,
        task_id=new_task_id(options.path),
    )
    manager = make_manager()
    stop_on_signals()
    try:
        return await run_task(manager, run)
    finally:
        await remove_sandbox(lambda: manager, run.task_id)


def stop_on_signals() -> None:
    """Make SIGTERM and SIGHUP stop the run the way Ctrl-C does.

    asyncio.run turns Ctrl-C into a cancel of the main task, so the finally block that removes
    the sandbox runs. SIGTERM (`kill`, a service manager) and SIGHUP (a closed terminal) would
    end the process on the spot instead, with the container still running.
    """
    task = asyncio.current_task()
    assert task is not None  # a coroutine always runs inside a task
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, task.cancel)


async def run_task(manager: WorkspaceManager, run: Run) -> int:
    options = run.options
    typer.echo(f"task {run.task_id}: {run.title}")
    typer.echo(
        f"repo local:{options.path}, provider {run.provider.name}, model {run.loop_cfg.model}"
    )
    setup = default_setup_command(options.path) or "nothing to run"
    typer.echo(f"preparing the workspace; setup runs with the network on: {setup}", err=True)
    sandbox, base_sha = await prepare_local_workspace(
        manager,
        options.path,
        task_id=run.task_id,
        image=run.cfg.sandbox.image,
        limits=run.cfg.sandbox.limits,
        setup_command=None,
    )
    typer.echo(f"workspace ready, base {base_sha[:7]}")
    spec = build_spec(run, base_sha)
    registry = default_registry(
        test_command=options.test_command, max_timeout_s=run.cfg.sandbox.command_timeout_s
    )
    result = await run_loop(spec, run.provider, registry, sandbox, run.loop_cfg, print_turn)
    outcome = result.outcome
    typer.echo(outcome_line(outcome.kind, result.turns, outcome.summary))
    await print_final_diff(registry, sandbox, spec, result.turns)
    return 0 if outcome.kind is OutcomeKind.PR_OPENED else EXIT_ERROR


async def remove_sandbox(make: Callable[[], WorkspaceManager], task_id: str) -> None:
    """Remove the container and the volume. A failure is reported and changes no result.

    The manager is built inside the guard: DockerSandboxManager() connects to the daemon, so a
    Docker that is down fails there and not in cleanup_task.
    """
    try:
        await make().cleanup_task(task_id)
    except Exception as exc:
        typer.echo(f"warning: could not remove the sandbox of task {task_id}: {exc}", err=True)


async def print_final_diff(
    registry: ToolRegistry, sandbox: Sandbox, spec: TaskSpec, turns: int
) -> None:
    ctx = ToolContext(task_id=spec.task_id, turn_index=turns, blob_prefix=f"tasks/{spec.task_id}")
    diff = await registry.run(ToolCall(id="final-diff", name="git_diff", input={}), sandbox, ctx)
    if not diff.ok:
        typer.echo(f"could not read the final diff: {diff.output}", err=True)
        return
    typer.echo(DIFF_HEADING)
    typer.echo(diff.output)


# ---------- the durable commands ----------
@dataclass(frozen=True)
class DurableOptions:
    path: Path
    issue_file: Path
    provider: ProviderName | None
    script: Path | None


async def open_client(settings: EnvSettings) -> Client:
    """The Temporal client. A function of its own so that unit tests can replace it."""
    return await connect(settings.temporal_address, namespace=settings.temporal_namespace)


def make_blobs(settings: EnvSettings) -> BlobStoreBase:
    """The blob store that the worker shares, which is also where the script of a task goes."""
    try:
        return S3BlobStore.from_settings(settings)
    except RuntimeError as exc:  # a setting is unset; the command reports it in one line
        raise ValueError(str(exc)) from exc


def make_store(blobs: BlobStoreBase, settings: EnvSettings) -> GitCheckpointStore:
    """The checkpoint store, for reading the diff. It is the folder the worker writes to."""
    return GitCheckpointStore(blobs, root=settings.ckpt_dir)


async def pause(seconds: float) -> None:
    """Wait between two polls. A function of its own so that unit tests need not wait."""
    await asyncio.sleep(seconds)


def resolve_script(options: DurableOptions, settings: EnvSettings) -> Path | None:
    """The script to send with the task, or None when the worker's own provider answers.

    Only the scripted provider has a script. For any other provider the worker uses the one in
    its own settings, and a script on the command line would be silently ignored, so it is an
    error.
    """
    name = options.provider.value if options.provider is not None else settings.provider
    if name != ProviderName.SCRIPTED.value:
        if options.script is not None:
            raise ValueError("--script only goes with the scripted provider")
        return None
    script = options.script or settings.script
    if script is None:
        raise ValueError(
            "the scripted provider needs a script: pass --script FILE or set LONGHAUL_SCRIPT"
        )
    return script


def durable_spec(path: Path, task_id: str, title: str, body: str) -> TaskSpec:
    # The worker reads the folder through this path, so it has to be the absolute one. The model
    # sees the folder name only (agent/prompts.py).
    repo = f"local:{path}"
    return TaskSpec(
        task_id=task_id,
        repo=repo,
        issue_number=None,
        title=title,
        body=body,
        settings=RepoSettings(repo=repo, test_command=DEFAULT_TEST_COMMAND),
    )


async def submit(options: DurableOptions) -> int:
    """Check every input, start the task and follow it. Nothing is started before the checks."""
    settings = EnvSettings()
    title, body = read_issue(options.issue_file)
    script = resolve_script(options, settings)
    blobs = make_blobs(settings)
    task_id = new_task_id(options.path)
    spec = durable_spec(options.path, task_id, title, body)
    script_ref: BlobRef | None = None
    if script is not None:
        script_ref = await upload_script(blobs, task_id, script)
    client = await open_client(settings)
    handle = await start_task(client, spec, task_queue=settings.task_queue, script=script_ref)

    who = "provider scripted" if script is not None else "provider of the worker"
    typer.echo(f"task {task_id}: {title}")
    typer.echo(f"repo {spec.repo}, {who}, task queue {settings.task_queue}")
    try:
        outcome = await follow(handle)
    except asyncio.CancelledError:
        typer.echo(
            f"interrupted. Task {task_id} is still running: follow it with "
            f"`longhaul status {task_id}` or stop it with `longhaul cancel {task_id}`",
            err=True,
        )
        raise
    except WorkflowFailureError as exc:
        typer.echo(f"error: the workflow failed: {failure_text(exc)}", err=True)
        await remove_sandbox(make_manager, task_id)
        return EXIT_ERROR

    final = await try_status(handle)
    typer.echo(outcome_line(outcome.kind, final.turn_index if final else None, outcome.summary))
    await print_task_diff(make_store(blobs, settings), task_id, final)
    # A finished workflow leaves its sandbox until LH-507 makes it destroy it. The cleanup is
    # safe to run twice, so this keeps working after that.
    await remove_sandbox(make_manager, task_id)
    return 0 if outcome.kind is OutcomeKind.PR_OPENED else EXIT_ERROR


async def follow(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    """Print the status of the task each time it changes, until the workflow ends.

    The end is read from the server (`describe`), which needs no worker. The status is read with
    a query, which does, so the line says when no worker answers. A worker that was stopped and
    started again is therefore visible here, and the run goes on.
    """
    last: str | None = None
    while (await handle.describe()).status in OPEN_STATES:
        line = status_line(await try_status(handle))
        if line != last:
            typer.echo(line)
            last = line
        await pause(POLL_S)
    return await handle.result()


async def try_status(handle: WorkflowHandle[Any, Outcome]) -> TaskStatus | None:
    """status() of the workflow, or None when no worker answers within STATUS_TIMEOUT_S."""
    try:
        return await read_status(handle, timeout_s=STATUS_TIMEOUT_S)
    except (RPCError, WorkflowQueryFailedError):
        return None


async def print_task_diff(
    store: GitCheckpointStore, task_id: str, final: TaskStatus | None
) -> None:
    """The diff of the run, read from the bare repo at the checkpoint the workflow ended on."""
    if final is None or final.last_checkpoint is None:
        typer.echo(
            "could not read the final diff: the workflow did not say where it ended", err=True
        )
        return
    try:
        patch = await store.diff(task_id, final.last_checkpoint)
    except (CheckpointError, ValueError) as exc:
        typer.echo(f"could not read the final diff: {exc}", err=True)
        return
    typer.echo(DIFF_HEADING)
    typer.echo(patch.rstrip("\n") if patch else NO_CHANGES)


async def show_status(task_id: str) -> None:
    handle = get_task(await open_client(EnvSettings()), task_id)
    state = (await handle.describe()).status
    name = state.name.lower().replace("_", " ") if state is not None else "in an unknown state"
    typer.echo(f"task {task_id}: {name}")
    current = await try_status(handle)
    if current is not None or state in OPEN_STATES:
        typer.echo(status_line(current))
    if state in OPEN_STATES:
        return
    try:
        outcome = await handle.result()
    except WorkflowFailureError as exc:
        typer.echo(f"failed: {failure_text(exc)}")
        return
    turns = current.turn_index if current is not None else None
    typer.echo(outcome_line(outcome.kind, turns, outcome.summary))


async def send_cancel(task_id: str, reason: str) -> None:
    await cancel_task(await open_client(EnvSettings()), task_id, by="cli", reason=reason)


# ---------- the poller commands ----------
def run_poller_command(command: Coroutine[Any, Any, None]) -> None:
    """Run one poller command and report a failure of the config or of Temporal in a line."""
    try:
        asyncio.run(command)
    except DURABLE_ERRORS as exc:
        typer.echo(f"error: {describe(exc)}", err=True)
        raise typer.Exit(EXIT_ERROR) from None


async def install_poller_schedule() -> None:
    settings = EnvSettings()
    interval = load_config(config_path(settings)).poller.interval_seconds
    outcome = await schedule.install_poller(
        await open_client(settings), interval_seconds=interval, task_queue=settings.task_queue
    )
    typer.echo(
        f"poller schedule {acts.POLLER_SCHEDULE_ID} {outcome.value}: a poll every {interval} s "
        f"on task queue {settings.task_queue}"
    )


async def pause_poller_schedule(note: str) -> None:
    await schedule.pause_poller(await open_client(EnvSettings()), note=note)
    typer.echo(f"poller schedule {acts.POLLER_SCHEDULE_ID} paused")


async def resume_poller_schedule() -> None:
    await schedule.resume_poller(await open_client(EnvSettings()))
    typer.echo(f"poller schedule {acts.POLLER_SCHEDULE_ID} resumed")


async def trigger_poller_schedule() -> None:
    await schedule.trigger_poller(await open_client(EnvSettings()))
    typer.echo(
        f"poll requested on schedule {acts.POLLER_SCHEDULE_ID}. "
        "Nothing happens if a poll is already running"
    )


def status_line(current: TaskStatus | None) -> str:
    """For example `status: coding, 3 turns done, cost $0.0000, checkpoint 1a2b3c4`."""
    if current is None:
        return "status: waiting for a worker to answer"
    turns = f"{current.turn_index} {'turn' if current.turn_index == 1 else 'turns'} done"
    line = f"status: {current.phase.value}, {turns}, cost ${current.cost_usd:.4f}"
    if current.last_checkpoint is not None:
        line += f", checkpoint {current.last_checkpoint[:7]}"
    return line


def failure_text(exc: BaseException) -> str:
    """The messages of an error and its causes, outermost first, as one line.

    Temporal wraps the error of an activity in an ActivityError whose own message says only that
    an activity failed. The reason is in the causes.
    """
    parts: list[str] = []
    error: BaseException | None = exc.cause if isinstance(exc, WorkflowFailureError) else exc
    while error is not None and len(parts) < 5:
        parts.append(str(error) or type(error).__name__)
        error = getattr(error, "cause", None)
    return ": ".join(parts)


# ---------- inputs ----------
def loop_config(cfg: AppConfig, options: RunOptions) -> LoopConfig:
    turns = options.max_turns or Budgets().task_turns
    loop_cfg = LoopConfig.from_config(cfg, Role.CODER, max_turns=turns)
    if options.model:
        loop_cfg = loop_cfg.model_copy(update={"model": options.model})
    return loop_cfg


def build_spec(run: Run, base_sha: str) -> TaskSpec:
    # The spec reaches the model in the system prompt. Name the folder only: the absolute
    # path would send the local user name and directory layout to the provider.
    repo = f"local:{run.options.path.name}"
    settings = RepoSettings(
        repo=repo,
        test_command=run.options.test_command,
        budgets=Budgets(task_turns=run.loop_cfg.max_turns),
    )
    return TaskSpec(
        task_id=run.task_id,
        repo=repo,
        issue_number=None,
        title=run.title,
        body=run.body,
        base_sha=base_sha,
        settings=settings,
    )


def split_issue(text: str) -> tuple[str, str]:
    """The title is the first line without its Markdown hashes. The rest is the body."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").strip().split("\n")
    title = lines[0].lstrip("#").strip()
    if not title:
        raise ValueError("the issue has no title: its first line is empty")
    return title, "\n".join(lines[1:]).strip()


def read_issue(path: Path) -> tuple[str, str]:
    """The title and the body of an issue file, with the body cut to the limit of a GitHub issue.

    The body goes into the spec of the task, and the spec is part of the carry state and the
    input of several activities. A 300 KB file would put 300 KB into each of them, so a local
    task is held to the limit that intake.py sets for a GitHub one.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot read {path}: it is not UTF-8 text ({exc.reason})") from exc
    try:
        title, body = split_issue(text)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc
    kept = clip_text(body, MAX_BODY_BYTES, FILE_CUT_NOTE)
    if kept != body:
        typer.echo(
            f"note: the body of {path} is {len(body.encode())} bytes, so the task keeps the "
            f"first {MAX_BODY_BYTES}",
            err=True,
        )
    return title, kept


def new_task_id(path: Path) -> str:
    """A task id that is a valid Docker name part, and new for every run."""
    slug = re.sub(r"[^a-z0-9]+", "-", path.name.lower()).strip("-")[:24].strip("-")
    return f"lh-local-{slug or 'repo'}-{uuid.uuid4().hex[:8]}"


# ---------- output ----------
def format_call(call: CallEvent) -> str:
    text = f"{call.name} {call.args}".rstrip()
    return text if call.ok else f"{text} [failed]"


def format_turn(event: TurnEvent) -> str:
    """For example `turn 3 coder: edit_file src/calc/__init__.py`. Turns count from 1."""
    parts = [", ".join(format_call(c) for c in event.calls)] if event.calls else []
    if event.note:
        parts.append(f"({event.note})")
    detail = " ".join(parts) or "(no tool calls)"
    return f"turn {event.turn_index + 1} {event.role.value}: {detail}"


def print_turn(event: TurnEvent) -> None:
    typer.echo(format_turn(event))


def outcome_line(kind: OutcomeKind, turns: int | None, summary: str) -> str:
    """For example `finished after 5 turns: Changed add()`. `turns` is None when it is not known."""
    verb = "finished" if kind is OutcomeKind.PR_OPENED else "stopped"
    if turns is None:
        return f"{verb}: {summary}"
    return f"{verb} after {turns} {'turn' if turns == 1 else 'turns'}: {summary}"


def describe(exc: Exception, task_id: str | None = None) -> str:
    """One message for an error of the run. Whoever reads it is at a terminal, not in a log."""
    if isinstance(exc, RPCError):
        if exc.status is RPCStatusCode.NOT_FOUND and task_id is not None:
            return f"task {task_id} was not found, or it has already ended"
        return f"temporal: {exc.message}"
    if isinstance(exc, WorkflowAlreadyStartedError):
        return f"task {exc.workflow_id} already exists on this Temporal server"
    if isinstance(exc, ProviderError):
        return f"the model provider failed ({exc.kind.value}): {exc.message}"
    if isinstance(exc, ImageNotFound):
        return f"{exc}. Build the sandbox image with `make sandbox-image`."
    if isinstance(exc, DockerException):
        return f"docker: {exc}"
    if isinstance(exc, DBAPIError):
        return f"database: {exc.orig}"  # without the SQL and the link to the SQLAlchemy docs
    if isinstance(exc, SQLAlchemyError):
        return f"database: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
    return str(exc) or type(exc).__name__


if __name__ == "__main__":
    app()
