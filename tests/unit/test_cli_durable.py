"""The durable commands of the CLI: `worker`, `run`, `status` and `cancel` (LH-208).

Temporal, the blob store, the checkpoint store and Docker are replaced by fakes, so the tests
need no services. A fake workflow handle plays a run: each call to `describe` is one poll of the
command, and the statuses it answers are the ones the test scripted. tests/e2e/test_durable_run.py
runs the same commands against the real stack.

The tests are plain functions on purpose: the commands start their own event loop with
asyncio.run, which cannot happen inside a running one.
"""

import asyncio
import os
import re
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from docker.errors import DockerException
from sqlalchemy.exc import OperationalError
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from typer.testing import CliRunner, Result

from longhaul import cli
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import RepoNotFoundError
from longhaul.context.modes import ContextMode
from longhaul.contracts import Outcome, OutcomeKind, Phase, TaskSpec, TaskStatus
from longhaul.db.context_totals import ContextTotals
from longhaul.durable import acts
from longhaul.durable.payload_guard import DEFAULT_LIMIT, json_size
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github.intake import FILE_CUT_NOTE, MAX_BODY_BYTES
from longhaul.settings import load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

# Rich styles the usage error when it detects CI (GITHUB_ACTIONS) or FORCE_COLOR, and the escape
# codes split option names, so read it as plain text.
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
SCRIPT = ROOT / "tests" / "fixtures" / "scripts" / "calc_fix.yaml"
RUNNER = CliRunner()
HEAD = "1a2b3c4d5e6f" + "0" * 28
SUMMARY = "Changed add() to return a + b. All tests pass."
FINISHED = Outcome(kind=OutcomeKind.PR_OPENED, summary=SUMMARY)
PATCH = (
    "diff --git a/src/calc/__init__.py b/src/calc/__init__.py\n"
    "-    return a - b\n"
    "+    return a + b\n"
)
DIFF_MARKER = "final diff against base:"
QUEUE = "lh-unit-queue"


# --- fakes ---------------------------------------------------------------------------------


def status(phase: Phase, turns: int = 0, checkpoint: str | None = HEAD) -> TaskStatus:
    return TaskStatus(
        task_id="lh-local-calc-0a1b2c3d",
        phase=phase,
        turn_index=turns,
        cost_usd=0.0,
        last_checkpoint=checkpoint,
        plan_version=None,
    )


def rpc_error(code: RPCStatusCode = RPCStatusCode.DEADLINE_EXCEEDED) -> RPCError:
    return RPCError("no worker answered", code, b"")


def failed_run(message: str) -> WorkflowFailureError:
    return WorkflowFailureError(cause=ApplicationError(message))


class FakeHandle:
    """A workflow handle that plays a run.

    `polls` holds what the status query answers while the run is open, one entry for each
    poll. An exception in the list is raised, as the SDK does when no worker answers. After the
    list is used up the run is closed, and `final` is what a query then answers.
    """

    def __init__(
        self,
        polls: list[TaskStatus | Exception],
        *,
        final: TaskStatus | Exception | None = None,
        outcome: Outcome | Exception = FINISHED,
        memo: dict[str, Any] | None = None,
    ) -> None:
        self.polls = polls
        self.memo = memo or {}
        self.final = final if final is not None else status(Phase.DONE, 5)
        self.outcome = outcome
        self.describes = 0
        self.queries: list[dict[str, Any]] = []

    @property
    def open(self) -> bool:
        return self.describes <= len(self.polls)

    async def describe(self) -> Any:
        self.describes += 1
        closed = (
            WorkflowExecutionStatus.FAILED
            if isinstance(self.outcome, WorkflowFailureError)
            else WorkflowExecutionStatus.COMPLETED
        )
        return SimpleNamespace(
            status=WorkflowExecutionStatus.RUNNING if self.open else closed,
            memo_value=self.memo_value,
        )

    async def memo_value(self, key: str, default: Any = None, **_: Any) -> Any:
        return self.memo.get(key, default)

    async def query(self, query: Any, **kwargs: Any) -> TaskStatus:
        assert query == TaskWorkflow.status
        self.queries.append(kwargs)
        answer = self.polls[self.describes - 1] if self.open else self.final
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def result(self) -> Outcome:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeTemporal:
    """The client: it hands out the handle for any task and notes what was started."""

    def __init__(self, handle: FakeHandle) -> None:
        self.handle = handle
        self.started: list[tuple[TaskSpec, dict[str, Any]]] = []
        self.states: list[Any] = []
        self.signals: list[tuple[Any, Any]] = []
        self.looked_up: list[str] = []
        self.lookup_error: Exception | None = None

    async def start_workflow(self, workflow: Any, state: Any, **kwargs: Any) -> FakeHandle:
        assert workflow == TaskWorkflow.run
        self.started.append((state.spec, kwargs))
        self.states.append(state)
        return self.handle

    def get_workflow_handle_for(self, workflow: Any, workflow_id: str) -> "FakeTemporal":
        self.looked_up.append(workflow_id)
        return self

    # The handle of a lookup is the fake itself, so its signals are noted here.
    async def signal(self, signal: Any, *, args: Any) -> None:
        if self.lookup_error is not None:
            raise self.lookup_error
        self.signals.append((signal, args))

    async def describe(self) -> Any:
        if self.lookup_error is not None:
            raise self.lookup_error
        return await self.handle.describe()

    async def query(self, query: Any, **kwargs: Any) -> TaskStatus:
        return await self.handle.query(query, **kwargs)

    async def result(self) -> Outcome:
        return await self.handle.result()


class FakeStore:
    """The host side of the checkpoint store: it can only answer `diff`."""

    def __init__(self, patch: str | Exception = PATCH) -> None:
        self.patch = patch
        self.asked: list[tuple[str, str]] = []

    async def diff(self, task_id: str, head_sha: str) -> str:
        self.asked.append((task_id, head_sha))
        if isinstance(self.patch, Exception):
            raise self.patch
        return self.patch


class FakeManager:
    def __init__(self) -> None:
        self.cleaned: list[str] = []
        self.error: Exception | None = None

    async def cleanup_task(self, task_id: str) -> None:
        if self.error is not None:
            raise self.error
        self.cleaned.append(task_id)


class Stack:
    """Everything the commands reach, replaced."""

    def __init__(self, handle: FakeHandle, store: FakeStore | None = None) -> None:
        self.temporal = FakeTemporal(handle)
        self.blobs = MemoryBlobStore()
        self.store = store or FakeStore()
        self.manager = FakeManager()
        self.pauses: list[float] = []
        # What the database says of the context of the task: None for a task it does not know.
        self.totals: ContextTotals | Exception | None = None
        self.totals_asked: list[tuple[str, int | None]] = []

    @property
    def spec(self) -> TaskSpec:
        ((spec, _),) = self.temporal.started
        return spec

    @property
    def start_kwargs(self) -> dict[str, Any]:
        ((_, kwargs),) = self.temporal.started
        return kwargs


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No settings from the machine, and no .env in the working directory."""
    for name in list(os.environ):
        if name.startswith("LONGHAUL_") or name in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("LONGHAUL_TASK_QUEUE", QUEUE)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def stack_for(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Stack]:
    def build(handle: FakeHandle, store: FakeStore | None = None) -> Stack:
        stack = Stack(handle, store)

        async def open_client(settings: Any) -> FakeTemporal:
            assert settings.task_queue == QUEUE
            return stack.temporal

        async def pause(seconds: float) -> None:
            stack.pauses.append(seconds)

        monkeypatch.setattr(cli, "open_client", open_client)
        monkeypatch.setattr(cli, "make_blobs", lambda settings: stack.blobs)
        monkeypatch.setattr(cli, "make_store", lambda blobs, settings: stack.store)
        monkeypatch.setattr(cli, "make_manager", lambda: stack.manager)
        monkeypatch.setattr(cli, "pause", pause)

        async def context_totals_of(task_id: str, window: int | None) -> ContextTotals | None:
            stack.totals_asked.append((task_id, window))
            if isinstance(stack.totals, Exception):
                raise stack.totals
            return stack.totals

        monkeypatch.setattr(cli, "context_totals_of", context_totals_of)
        return stack

    return build


def run_args(*extra: str, script: Path | None = SCRIPT) -> list[str]:
    args = ["run", "--path", str(CALC), "--issue-file", str(CALC / "ISSUE.md")]
    if script is not None:
        args += ["--provider", "scripted", "--script", str(script)]
    return [*args, *extra]


def invoke(args: list[str]) -> Result:
    return RUNNER.invoke(cli.app, args)


def happy() -> FakeHandle:
    return FakeHandle([status(Phase.PREPARING, checkpoint=None), status(Phase.CODING, 3)])


# --- run: what is started ------------------------------------------------------------------


def test_run_starts_a_task_for_the_local_folder(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())
    result = invoke(run_args())
    assert result.exit_code == 0, result.output

    spec = stack.spec
    assert spec.repo == f"local:{CALC}"
    assert spec.settings.repo == spec.repo
    assert spec.title == "add() returns the difference"
    assert "`calc.add` subtracts its arguments" in spec.body
    assert spec.issue_number is None
    assert spec.settings.test_command == cli.DEFAULT_TEST_COMMAND
    assert stack.start_kwargs["task_queue"] == QUEUE
    assert stack.start_kwargs["id"] == spec.task_id


def test_run_cuts_a_300_kb_issue_file_before_it_starts_the_task(
    stack_for: Callable[..., Stack], tmp_path: Path
) -> None:
    stack = stack_for(happy())
    issue = tmp_path / "big.md"
    issue.write_text("# Big issue\n\n" + "x" * 300_000, encoding="utf-8")
    args = run_args(script=SCRIPT)
    args[args.index("--issue-file") + 1] = str(issue)

    result = invoke(args)

    assert result.exit_code == 0, result.output
    spec = stack.spec
    assert spec.title == "Big issue"
    assert spec.body == "x" * MAX_BODY_BYTES + FILE_CUT_NOTE
    # The carry state holds the spec and goes into the history at the start. A cut body keeps
    # it under the 16 KB of CONTRIBUTING.md rule 5, far below the 256 KB cap.
    assert json_size(spec) < 16_000 < DEFAULT_LIMIT


def test_the_task_id_is_the_folder_name_and_a_suffix_that_is_new_for_every_run(
    stack_for: Callable[..., Stack],
) -> None:
    ids = []
    for _ in range(2):
        stack = stack_for(happy())
        assert invoke(run_args()).exit_code == 0
        ids.append(stack.spec.task_id)
    assert ids[0] != ids[1]
    for task_id in ids:
        assert task_id.startswith("lh-local-calc-") and len(task_id) == len("lh-local-calc-") + 8


def test_the_script_is_uploaded_under_the_task_and_named_in_the_memo(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    assert invoke(run_args()).exit_code == 0

    task_id = stack.spec.task_id
    (ref,) = stack.start_kwargs["memo"].values()
    assert list(stack.start_kwargs["memo"]) == [acts.SCRIPT_MEMO_KEY]
    assert ref.key == f"tasks/{task_id}/script.yaml"
    assert stack.blobs.objects[ref.key] == SCRIPT.read_bytes()


def test_a_window_goes_to_the_worker_in_the_tuning_of_the_task(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--window", "8000"))
    assert result.exit_code == 0, result.output

    memo = stack.start_kwargs["memo"]
    assert set(memo) == {acts.SCRIPT_MEMO_KEY, acts.TUNING_MEMO_KEY}
    assert memo[acts.TUNING_MEMO_KEY] == acts.Tuning(window_tokens=8_000)


def test_the_header_says_which_window_the_run_asked_for(
    stack_for: Callable[..., Stack],
) -> None:
    stack_for(happy())
    result = invoke(run_args("--window", "8000"))
    assert "context window 8000 tokens" in result.stdout


def test_a_run_without_a_window_has_no_tuning_and_the_worker_config_decides(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args())
    assert acts.TUNING_MEMO_KEY not in stack.start_kwargs["memo"]
    assert "context window" not in result.stdout


def test_a_window_works_with_a_provider_of_the_worker(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--provider", "anthropic", "--window", "50000", script=None))
    assert result.exit_code == 0, result.output
    assert stack.start_kwargs["memo"] == {acts.TUNING_MEMO_KEY: acts.Tuning(window_tokens=50_000)}


@pytest.mark.parametrize("window", ["0", "-1", "many"])
def test_a_window_that_cannot_work_stops_the_command_before_anything_starts(
    stack_for: Callable[..., Stack], window: str
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--window", window))

    assert result.exit_code == 2
    assert "--window" in ANSI.sub("", result.output)
    assert stack.temporal.started == [] and stack.blobs.objects == {}


def test_the_script_and_the_provider_can_come_from_the_environment(
    stack_for: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = stack_for(happy())
    monkeypatch.setenv("LONGHAUL_PROVIDER", "scripted")
    monkeypatch.setenv("LONGHAUL_SCRIPT", str(SCRIPT))
    assert invoke(run_args(script=None)).exit_code == 0
    assert list(stack.start_kwargs["memo"]) == [acts.SCRIPT_MEMO_KEY]


def test_a_context_mode_goes_into_the_memo_with_the_script(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--context-mode", "no_compaction"))
    assert result.exit_code == 0, result.output

    memo = stack.start_kwargs["memo"]
    assert list(memo) == [acts.SCRIPT_MEMO_KEY, acts.CONTEXT_MODE_MEMO_KEY]
    assert memo[acts.CONTEXT_MODE_MEMO_KEY] is ContextMode.NO_COMPACTION


def test_a_context_mode_goes_with_a_provider_of_the_worker_too(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--provider", "anthropic", "--context-mode", "none", script=None))
    assert result.exit_code == 0, result.output
    assert stack.start_kwargs["memo"] == {acts.CONTEXT_MODE_MEMO_KEY: ContextMode.NONE}


def test_without_a_context_mode_the_worker_config_decides(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    assert invoke(run_args()).exit_code == 0
    assert acts.CONTEXT_MODE_MEMO_KEY not in stack.start_kwargs["memo"]


def test_a_run_enforces_the_budgets_of_the_task_unless_told_not_to(
    stack_for: Callable[..., Stack],
) -> None:
    # A real provider costs money, and nobody watches a long run for the bill.
    stack = stack_for(happy())
    assert invoke(run_args()).exit_code == 0
    assert [state.enforce_budgets for state in stack.temporal.states] == [True]

    stack = stack_for(happy())
    assert invoke(run_args("--no-budgets")).exit_code == 0
    assert [state.enforce_budgets for state in stack.temporal.states] == [False]


def test_the_header_says_when_the_budgets_are_off(stack_for: Callable[..., Stack]) -> None:
    stack_for(happy())
    assert "budgets off" in invoke(run_args("--no-budgets")).output

    stack_for(happy())
    assert "budgets off" not in invoke(run_args()).output


def test_the_header_names_the_mode_the_run_asked_for(stack_for: Callable[..., Stack]) -> None:
    stack_for(happy())
    named = invoke(run_args("--context-mode", "none"))
    assert "context mode none" in named.output

    stack_for(happy())
    unnamed = invoke(run_args())
    assert "context mode" not in unnamed.output


def test_a_context_mode_that_does_not_exist_is_a_usage_error_and_starts_nothing(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--context-mode", "partial"))

    assert result.exit_code == 2
    assert "--context-mode" in ANSI.sub("", result.output)
    assert stack.temporal.started == []


def test_a_real_provider_uploads_no_script_and_the_worker_uses_its_own(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--provider", "anthropic", script=None))
    assert result.exit_code == 0, result.output
    assert stack.start_kwargs["memo"] is None
    assert stack.blobs.objects == {}
    assert "provider of the worker" in result.stdout


def test_the_scripted_provider_without_a_script_stops_the_command_before_anything_starts(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--provider", "scripted", script=None))

    assert result.exit_code == 2
    assert "needs a script" in result.output
    assert stack.temporal.started == [] and stack.blobs.objects == {}


def test_a_script_with_a_real_provider_stops_the_command_before_anything_starts(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--provider", "anthropic", "--script", str(SCRIPT), script=None))

    assert result.exit_code == 2
    assert "only goes with the scripted provider" in result.output
    assert stack.temporal.started == [] and stack.blobs.objects == {}


def test_a_script_that_does_not_load_stops_the_command_before_anything_starts(
    stack_for: Callable[..., Stack], tmp_path: Path
) -> None:
    stack = stack_for(happy())
    bad = tmp_path / "bad.yaml"
    bad.write_text("steps: 3\n", encoding="utf-8")

    result = invoke(run_args(script=bad))

    assert result.exit_code == 2
    assert "bad.yaml" in result.output
    assert stack.temporal.started == [] and stack.blobs.objects == {}


def test_a_missing_folder_or_issue_file_is_a_usage_error(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())
    assert invoke(["run", "--path", "/no/such/folder", "--issue-file", "x"]).exit_code == 2
    assert stack.temporal.started == []


# --- run: what is printed ------------------------------------------------------------------


def test_run_prints_the_header_the_status_changes_the_outcome_and_the_diff(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    result = invoke(run_args())
    assert result.exit_code == 0, result.output

    lines = result.stdout.splitlines()
    task_id = stack.spec.task_id
    assert lines[0] == f"task {task_id}: add() returns the difference"
    assert lines[1] == f"repo local:{CALC}, provider scripted, task queue {QUEUE}"
    assert lines[2:] == [
        "status: preparing, 0 turns done, cost $0.0000",
        "status: coding, 3 turns done, cost $0.0000, checkpoint 1a2b3c4",
        f"finished after 5 turns: {SUMMARY}",
        DIFF_MARKER,
        *PATCH.rstrip("\n").splitlines(),
    ]


def test_a_status_that_has_not_changed_is_not_printed_again(
    stack_for: Callable[..., Stack],
) -> None:
    same = status(Phase.CODING, 2)
    stack_for(FakeHandle([same, same, same, status(Phase.CODING, 3)]))
    result = invoke(run_args())
    assert [ln for ln in result.stdout.splitlines() if ln.startswith("status:")] == [
        "status: coding, 2 turns done, cost $0.0000, checkpoint 1a2b3c4",
        "status: coding, 3 turns done, cost $0.0000, checkpoint 1a2b3c4",
    ]


def test_run_polls_every_two_seconds_with_a_deadline_on_the_query(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    invoke(run_args())

    assert cli.POLL_S == 2.0
    assert stack.pauses == [2.0, 2.0]
    deadlines = [kwargs["rpc_timeout"].total_seconds() for kwargs in stack.temporal.handle.queries]
    assert deadlines == [cli.STATUS_TIMEOUT_S] * 3  # two polls and the final status


def test_a_poll_that_no_worker_answers_is_shown_once_and_the_run_goes_on(
    stack_for: Callable[..., Stack],
) -> None:
    stack_for(
        FakeHandle(
            [
                status(Phase.CODING, 2),
                rpc_error(),
                rpc_error(),
                rpc_error(),
                status(Phase.CODING, 4),
            ]
        )
    )
    result = invoke(run_args())

    assert result.exit_code == 0, result.output
    assert [ln for ln in result.stdout.splitlines() if ln.startswith("status:")] == [
        "status: coding, 2 turns done, cost $0.0000, checkpoint 1a2b3c4",
        "status: waiting for a worker to answer",
        "status: coding, 4 turns done, cost $0.0000, checkpoint 1a2b3c4",
    ]


def test_the_outcome_line_uses_the_turns_of_the_last_status(
    stack_for: Callable[..., Stack],
) -> None:
    stack_for(FakeHandle([status(Phase.CODING, 1)], final=status(Phase.DONE, 7)))
    assert "finished after 7 turns:" in invoke(run_args()).stdout


def test_when_the_final_status_cannot_be_read_the_outcome_line_has_no_turn_count(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(FakeHandle([status(Phase.CODING, 1)], final=rpc_error()))
    result = invoke(run_args())
    assert f"finished: {SUMMARY}" in result.stdout.splitlines()
    # With no status the head is unknown, so the diff is not asked for.
    assert stack.store.asked == []


# --- run: the diff and the sandbox ---------------------------------------------------------


def test_the_diff_is_read_at_the_checkpoint_the_workflow_ended_on(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(FakeHandle([status(Phase.CODING, 1)], final=status(Phase.DONE, 5, HEAD)))
    invoke(run_args())
    assert stack.store.asked == [(stack.spec.task_id, HEAD)]


def test_an_empty_diff_says_so(stack_for: Callable[..., Stack]) -> None:
    stack_for(happy(), FakeStore(""))
    result = invoke(run_args())
    assert result.exit_code == 0
    assert result.stdout.splitlines()[-2:] == [DIFF_MARKER, "no changes against base"]


def test_a_repo_that_is_gone_is_reported_and_does_not_change_the_exit_code(
    stack_for: Callable[..., Stack],
) -> None:
    stack_for(happy(), FakeStore(RepoNotFoundError("task x has no checkpoint repo")))
    result = invoke(run_args())
    assert result.exit_code == 0
    assert DIFF_MARKER not in result.stdout
    assert "could not read the final diff" in result.stderr


def test_the_sandbox_of_the_task_is_removed_when_the_run_ends(
    stack_for: Callable[..., Stack],
) -> None:
    # The workflow destroys it too (LH-507). The command makes sure, and the cleanup is safe twice.
    stack = stack_for(happy())
    invoke(run_args())
    assert stack.manager.cleaned == [stack.spec.task_id]


def test_a_sandbox_that_cannot_be_removed_is_a_warning_and_not_a_failure(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    stack.manager.error = OSError("docker went away")
    result = invoke(run_args())
    assert result.exit_code == 0
    assert "could not remove the sandbox" in result.stderr


def test_docker_that_cannot_be_reached_for_the_cleanup_is_a_warning_and_not_a_failure(
    stack_for: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Building the manager connects to the daemon, so the failure comes before cleanup_task.
    stack = stack_for(happy())

    def unreachable() -> Any:
        raise DockerException("Error while fetching server API version: connection refused")

    monkeypatch.setattr(cli, "make_manager", unreachable)
    result = invoke(run_args())

    assert result.exit_code == 0, result.output
    assert DIFF_MARKER in result.stdout
    assert "could not remove the sandbox" in result.stderr
    assert "Error while fetching server API version" in result.stderr
    assert stack.manager.cleaned == []


def test_docker_that_cannot_be_reached_after_a_failed_workflow_keeps_the_cause(
    stack_for: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    stack_for(
        FakeHandle([status(Phase.CODING, 2)], outcome=failed_run("no checkpoint to return to"))
    )

    def unreachable() -> Any:
        raise DockerException("Error while fetching server API version: connection refused")

    monkeypatch.setattr(cli, "make_manager", unreachable)
    result = invoke(run_args())

    assert result.exit_code == 2
    assert "no checkpoint to return to" in result.stderr
    assert "could not remove the sandbox" in result.stderr


# --- run: how it ends ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome",
    [
        Outcome(kind=OutcomeKind.ERROR, summary="turn limit"),
        Outcome(kind=OutcomeKind.CANCELLED, summary="cancelled by cli: wrong repo"),
        Outcome(kind=OutcomeKind.STUCK, summary="stuck"),
    ],
    ids=lambda outcome: outcome.kind.value,
)
def test_an_outcome_other_than_finish_exits_2_and_still_prints_what_there_is(
    stack_for: Callable[..., Stack], outcome: Outcome
) -> None:
    stack = stack_for(FakeHandle([status(Phase.CODING, 2)], outcome=outcome))
    result = invoke(run_args())

    assert result.exit_code == 2
    assert f"stopped after 5 turns: {outcome.summary}" in result.stdout.splitlines()
    assert DIFF_MARKER in result.stdout
    assert stack.manager.cleaned == [stack.spec.task_id]


def test_a_workflow_that_failed_exits_2_with_the_cause_and_removes_the_sandbox(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(
        FakeHandle([status(Phase.CODING, 2)], outcome=failed_run("no checkpoint to return to"))
    )
    result = invoke(run_args())

    assert result.exit_code == 2
    assert "the workflow failed" in result.stderr
    assert "no checkpoint to return to" in result.stderr
    assert DIFF_MARKER not in result.stdout
    assert stack.manager.cleaned == [stack.spec.task_id]


def test_ctrl_c_leaves_the_task_running_and_says_how_to_follow_or_stop_it(
    stack_for: Callable[..., Stack],
) -> None:
    handle = happy()

    async def interrupted() -> Any:
        raise asyncio.CancelledError

    handle.describe = interrupted  # type: ignore[method-assign]
    stack = stack_for(handle)

    result = invoke(run_args())

    assert result.exit_code == 2
    task_id = stack.spec.task_id
    assert "interrupted" in result.stderr
    assert f"longhaul status {task_id}" in result.stderr
    assert f"longhaul cancel {task_id}" in result.stderr
    assert stack.manager.cleaned == [], "the task still runs, so its sandbox is in use"


def test_a_temporal_server_that_cannot_be_reached_is_a_one_line_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(settings: Any) -> Any:
        raise ConnectionError("cannot reach Temporal at localhost:1: connection refused")

    monkeypatch.setattr(cli, "open_client", refuse)
    monkeypatch.setattr(cli, "make_blobs", lambda settings: MemoryBlobStore())

    result = invoke(run_args())

    assert result.exit_code == 2
    assert "cannot reach Temporal" in result.stderr
    assert "Traceback" not in result.output


def test_a_task_id_that_is_taken_is_reported(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())

    async def taken(*args: Any, **kwargs: Any) -> Any:
        raise WorkflowAlreadyStartedError("lh-local-calc-0a1b2c3d", "TaskWorkflow")

    stack.temporal.start_workflow = taken  # type: ignore[method-assign]
    result = invoke(run_args())
    assert result.exit_code == 2
    assert "already" in result.stderr


# --- status --------------------------------------------------------------------------------


def test_status_of_a_running_task_prints_its_state_and_its_status(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(FakeHandle([status(Phase.CODING, 3)] * 3))
    result = invoke(["status", "lh-local-calc-0a1b2c3d"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "task lh-local-calc-0a1b2c3d: running",
        "status: coding, 3 turns done, cost $0.0000, checkpoint 1a2b3c4",
    ]
    assert stack.temporal.looked_up == ["lh-local-calc-0a1b2c3d"]


def test_status_of_a_finished_task_adds_its_outcome(stack_for: Callable[..., Stack]) -> None:
    stack_for(FakeHandle([], final=status(Phase.DONE, 5)))
    result = invoke(["status", "lh-local-calc-0a1b2c3d"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "task lh-local-calc-0a1b2c3d: completed",
        "status: done, 5 turns done, cost $0.0000, checkpoint 1a2b3c4",
        f"finished after 5 turns: {SUMMARY}",
    ]


def test_status_without_a_worker_still_says_what_temporal_knows(
    stack_for: Callable[..., Stack],
) -> None:
    stack_for(FakeHandle([rpc_error()] * 3))
    result = invoke(["status", "lh-local-calc-0a1b2c3d"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "task lh-local-calc-0a1b2c3d: running",
        "status: waiting for a worker to answer",
    ]


def test_status_of_a_task_that_failed_prints_the_failure(stack_for: Callable[..., Stack]) -> None:
    stack_for(FakeHandle([], final=rpc_error(), outcome=failed_run("boom")))
    result = invoke(["status", "lh-local-calc-0a1b2c3d"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0] == "task lh-local-calc-0a1b2c3d: failed"
    assert "boom" in result.stdout


def test_status_of_an_unknown_task_is_an_error(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())
    stack.temporal.lookup_error = rpc_error(RPCStatusCode.NOT_FOUND)
    result = invoke(["status", "lh-nothing"])

    assert result.exit_code == 2
    assert "lh-nothing" in result.stderr


# --- status: the context of the task (LH-407) -------------------------------------------------
TASK_ID = "lh-local-calc-0a1b2c3d"


def test_status_of_a_finished_task_shows_its_compactions_and_its_fullest_request(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(FakeHandle([], final=status(Phase.DONE, 5)))
    stack.totals = ContextTotals(compactions=5, max_fraction=0.5862)

    result = invoke(["status", TASK_ID])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"task {TASK_ID}: completed",
        "status: done, 5 turns done, cost $0.0000, checkpoint 1a2b3c4",
        "context: 5 compactions, max_fraction 0.59",
        f"finished after 5 turns: {SUMMARY}",
    ]
    assert [task for task, _ in stack.totals_asked] == [TASK_ID]


def test_status_of_a_running_task_shows_them_too(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(FakeHandle([status(Phase.CODING, 3)] * 3))
    stack.totals = ContextTotals(compactions=1, max_fraction=0.31)

    result = invoke(["status", TASK_ID])

    assert result.stdout.splitlines() == [
        f"task {TASK_ID}: running",
        "status: coding, 3 turns done, cost $0.0000, checkpoint 1a2b3c4",
        "context: 1 compaction, max_fraction 0.31",
    ]


def test_status_shows_the_compactions_when_the_window_is_not_known(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(FakeHandle([status(Phase.CODING, 3)] * 3))
    stack.totals = ContextTotals(compactions=0, max_fraction=None)

    result = invoke(["status", TASK_ID])

    assert result.stdout.splitlines()[-1] == "context: 0 compactions"


def test_status_has_no_context_line_for_a_task_the_database_does_not_know(
    stack_for: Callable[..., Stack],
) -> None:
    stack_for(FakeHandle([status(Phase.CODING, 3)] * 3))  # the totals stay None

    result = invoke(["status", TASK_ID])

    assert not [ln for ln in result.stdout.splitlines() if ln.startswith("context:")]


def test_status_still_answers_when_the_database_cannot_be_read(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(FakeHandle([], final=status(Phase.DONE, 5)))
    stack.totals = OperationalError("SELECT 1", {}, ConnectionRefusedError("refused"))

    result = invoke(["status", TASK_ID])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"task {TASK_ID}: completed",
        "status: done, 5 turns done, cost $0.0000, checkpoint 1a2b3c4",
        f"finished after 5 turns: {SUMMARY}",
    ]
    assert "could not read the context totals" in result.stderr
    assert "refused" in result.stderr


def test_the_totals_are_read_against_the_window_the_run_asked_for(
    stack_for: Callable[..., Stack],
) -> None:
    memo = {acts.TUNING_MEMO_KEY: acts.Tuning(window_tokens=8_000)}
    stack = stack_for(FakeHandle([status(Phase.CODING, 3)] * 3, memo=memo))

    invoke(["status", TASK_ID])

    assert stack.totals_asked == [(TASK_ID, 8_000)]


def test_a_run_without_a_window_of_its_own_is_read_against_the_window_of_the_config(
    stack_for: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LONGHAUL_CONFIG", str(ROOT / "config" / "longhaul.example.yaml"))
    stack = stack_for(FakeHandle([status(Phase.CODING, 3)] * 3))

    invoke(["status", TASK_ID])

    window = load_config(ROOT / "config" / "longhaul.example.yaml").context.window_tokens
    assert stack.totals_asked == [(TASK_ID, window)]


def test_a_config_that_cannot_be_read_leaves_the_window_unknown(
    stack_for: Callable[..., Stack], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LONGHAUL_CONFIG", str(tmp_path / "missing.yaml"))
    stack = stack_for(FakeHandle([status(Phase.CODING, 3)] * 3))

    result = invoke(["status", TASK_ID])

    assert result.exit_code == 0, result.output
    assert stack.totals_asked == [(TASK_ID, None)]


def test_without_a_database_setting_there_are_no_totals_to_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unset(*preferred: str | None) -> str:
        raise RuntimeError("LONGHAUL_DATABASE_URL is not set")

    monkeypatch.setattr(cli, "database_url", unset)

    assert asyncio.run(cli.context_totals_of(TASK_ID, 8_000)) is None


# --- cancel --------------------------------------------------------------------------------


def test_cancel_signals_the_workflow_with_the_reason(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())
    result = invoke(["cancel", "lh-local-calc-0a1b2c3d", "--reason", "wrong repo"])

    assert result.exit_code == 0, result.output
    assert stack.temporal.signals == [(TaskWorkflow.cancel, ["cli", "wrong repo"])]
    assert "lh-local-calc-0a1b2c3d" in result.stdout


def test_cancel_needs_no_reason(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())
    assert invoke(["cancel", "lh-local-calc-0a1b2c3d"]).exit_code == 0
    assert stack.temporal.signals == [(TaskWorkflow.cancel, ["cli", ""])]


def test_cancel_of_a_task_that_is_not_running_is_an_error(
    stack_for: Callable[..., Stack],
) -> None:
    stack = stack_for(happy())
    stack.temporal.lookup_error = rpc_error(RPCStatusCode.NOT_FOUND)
    result = invoke(["cancel", "lh-nothing"])

    assert result.exit_code == 2
    assert "lh-nothing" in result.stderr


# --- worker --------------------------------------------------------------------------------


def test_worker_runs_the_worker_process(monkeypatch: pytest.MonkeyPatch) -> None:
    from longhaul.durable import worker

    calls: list[str] = []

    async def fake_main() -> None:
        calls.append("main")

    monkeypatch.setattr(worker, "main", fake_main)
    result = invoke(["worker"])
    assert result.exit_code == 0, result.output
    assert calls == ["main"]


def test_worker_reports_a_missing_setting_in_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    from longhaul.durable import worker

    async def refuse() -> None:
        raise ValueError("LONGHAUL_DATABASE_URL is not set")

    monkeypatch.setattr(worker, "main", refuse)
    result = invoke(["worker"])
    assert result.exit_code == 2
    assert "LONGHAUL_DATABASE_URL is not set" in result.stderr
    assert "Traceback" not in result.output


def test_the_help_lists_every_command() -> None:
    result = invoke(["--help"])
    for name in ("run-local", "worker", "run", "status", "cancel"):
        assert name in result.stdout


# ---------- LH-501: the plan ----------
def test_run_does_not_plan_unless_asked(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())
    result = invoke(run_args())
    assert result.exit_code == 0, result.output
    assert list(stack.start_kwargs["memo"]) == [acts.SCRIPT_MEMO_KEY]


def test_run_plans_first_with_the_plan_flag(stack_for: Callable[..., Stack]) -> None:
    stack = stack_for(happy())
    result = invoke(run_args("--plan"))
    assert result.exit_code == 0, result.output
    assert stack.start_kwargs["memo"][acts.PLAN_MEMO_KEY] is True


def test_the_status_line_shows_the_version_of_the_plan() -> None:
    planned = status(Phase.CODING, 4).model_copy(update={"plan_version": 1})
    assert cli.status_line(planned) == (
        "status: coding, 4 turns done, cost $0.0000, plan v1, checkpoint 1a2b3c4"
    )
    assert "plan" not in cli.status_line(status(Phase.CODING, 4))
