"""The `longhaul` command line.

Today it has one command, `run-local`: copy a folder into a Docker sandbox, let the agent work
on the issue in a text file, and print what it did. No Temporal and no database are involved,
so it is the quickest way to watch the agent work.

What it prints, on stdout in this order: a header with the task id, the repo, the provider and
the model; one line per turn; one line for how the run ended; and the final `git_diff`.
Progress notes, warnings and errors go to stderr, so the output can be piped on.

Exit codes. 0: the agent called `finish`. 2: the run failed or ended in error (turn limit, no
tool calls, a refusal, a failing setup, a provider error, a missing Docker or image), or it
was interrupted. Bad options exit 2 as well, which is what Typer does for usage errors. An
unexpected exception is a bug in longhaul and exits 1 with its traceback.

The sandbox is removed at the end, together with its volume, whatever happened: a finish, an
error, a bug, Ctrl-C, SIGTERM or SIGHUP. Only SIGKILL and a crash of the machine leave one
behind. Its container and volume carry the task id, which the header line prints.
"""

import asyncio
import re
import signal
import uuid
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import typer
import yaml
from docker.errors import DockerException, ImageNotFound

from longhaul.agent.loop import CallEvent, LoopConfig, TurnEvent, run_loop
from longhaul.agent.workspace import (
    WorkspaceError,
    WorkspaceManager,
    default_setup_command,
    prepare_local_workspace,
)
from longhaul.contracts import (
    Budgets,
    LLMProvider,
    OutcomeKind,
    ProviderError,
    RepoSettings,
    Role,
    Sandbox,
    TaskSpec,
    ToolCall,
    ToolContext,
)
from longhaul.providers.base import get_provider
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.settings import AppConfig, EnvSettings, load_config
from longhaul.tools.registry import ToolRegistry, default_registry

DEFAULT_TEST_COMMAND = "python -m pytest -q"
DIFF_HEADING = "final diff against base:"
EXIT_ERROR = 2

# longhaul/cli.py -> longhaul -> src -> the checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

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

app = typer.Typer(
    name="longhaul",
    add_completion=False,
    no_args_is_help=True,
    # A traceback with locals could print the API key held in the settings.
    pretty_exceptions_enable=False,
)


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
        await remove_sandbox(manager, run.task_id)


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


async def remove_sandbox(manager: WorkspaceManager, task_id: str) -> None:
    """Remove the container and the volume. A failure is reported and changes no result."""
    try:
        await manager.cleanup_task(task_id)
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


# ---------- inputs ----------
def config_path(settings: EnvSettings) -> Path:
    """The config file: the one named by LONGHAUL_CONFIG, found from any working directory.

    The default is a path relative to the repo root. Run from another folder it would be
    missing, so the checkout this package lives in is tried next.
    """
    path = settings.config
    if path.is_absolute() or path.exists():
        return path
    bundled = PROJECT_ROOT / path
    return bundled if bundled.exists() else path


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
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"cannot read {path}: it is not UTF-8 text ({exc.reason})") from exc
    try:
        return split_issue(text)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


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


def outcome_line(kind: OutcomeKind, turns: int, summary: str) -> str:
    verb = "finished" if kind is OutcomeKind.PR_OPENED else "stopped"
    return f"{verb} after {turns} {'turn' if turns == 1 else 'turns'}: {summary}"


def describe(exc: Exception) -> str:
    """One message for an error of the run. Whoever reads it is at a terminal, not in a log."""
    if isinstance(exc, ProviderError):
        return f"the model provider failed ({exc.kind.value}): {exc.message}"
    if isinstance(exc, ImageNotFound):
        return f"{exc}. Build the sandbox image with `make sandbox-image`."
    if isinstance(exc, DockerException):
        return f"docker: {exc}"
    return str(exc) or type(exc).__name__


if __name__ == "__main__":
    app()
