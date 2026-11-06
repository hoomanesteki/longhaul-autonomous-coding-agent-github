"""TaskWorkflow against a time skipping Temporal test server, with stub activities.

The stubs take the place of the activities. They are registered under the names in acts.py, so
the workflow cannot tell them from the real ones. These tests check what the workflow itself
does with what an activity returns: the phases, the signals, the order of the calls, how each
stop reason is handled, how a sandbox that is gone is recreated and how a long run hands over to
a new one (Continue-As-New). test_activities.py runs the
real activities, test_sandbox_recovery.py runs the recovery on real containers, and
test_parity.py compares the whole loop with the in-process one. Each test gets its own server
and worker, so nothing is shared between tests. The first run downloads the test server (about
65 MB, ADR-0004); after that it starts in under a second.

The worker runs under the same sandbox settings as a production worker (invalid_modules for
the I/O libraries), so every test here also shows that the workflow loads under them.

A task with an issue number on an owner/name repo is delivered as a pull request (LH-306): it is
picked up with a comment and a label, and ends with build_pr_branch, deliver and the label swap.
The stubs for those activities log apart from the others (`github_names`), so the exact lists of
the loop's own activities in the tests below stay what they were. The default spec has no issue
number and so is not delivered; `github_spec` has one.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from longhaul.agent.rules import BAD_FINISH, MAX_PAUSES, NUDGE
from longhaul.context.tokens import Decision
from longhaul.contracts import (
    BlobRef,
    Budgets,
    CarryState,
    Outcome,
    OutcomeKind,
    Phase,
    ProgressNotes,
    RepoSettings,
    StopDetails,
    StopReason,
    TaskSpec,
    TaskStatus,
    ToolResult,
    Usage,
)
from longhaul.durable import acts
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github import lifecycle
from longhaul.github.inputs import CommentInput, LabelInput

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

BASE = "b" * 40
WAIT_S = 30.0
# A cancel reaches a running activity at its next heartbeat, and the SDK sends one at most
# every 0.8 heartbeat timeouts. The timeouts of section 3.7 would make each cancel test wait
# 24 s or more.
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=2)


PR_URL = "https://github.com/acme/calc/pull/7"
# The names of the activities of a GitHub task that the stubs log apart from the loop's own.
GITHUB_NAMES = frozenset(act.name for act in acts.GITHUB)


def sha(n: int) -> str:
    return f"{n:040x}"


def ref(name: str) -> BlobRef:
    return BlobRef(key=f"tasks/lh-acme-calc-42/{name}", sha256="0" * 64, size=1)


def call(name: str, call_id: str, **arguments: Any) -> acts.ToolCallSummary:
    return acts.ToolCallSummary(id=call_id, name=name, input=arguments)


def finish(summary: str = "fixed add()") -> acts.ToolCallSummary:
    return call(acts.FINISH_TOOL, "finish-1", summary=summary)


Script = Callable[[int], list[acts.ToolCallSummary]]


def never_finishes(turn: int) -> list[acts.ToolCallSummary]:
    return [call("read_file", f"read-{turn}", path="calc.py")]


class Stubs:
    """Stand-ins for the activities. They record every call and answer from a script.

    A gate holds an activity until the test opens it, which keeps the workflow in one place
    long enough to look at it. Gates start open. While a gate is closed the stub heartbeats, as
    a real activity does, because that is how a cancel reaches an activity. When one is
    cancelled it logs "<name>:cancelled".

    `gone` maps a call id to the number of times that call finds its sandbox gone before it
    runs. `ensure_sandbox` answers with a new id each time: sbx-2, then sbx-3, and so on.

    A turn gate holds the model call of one turn (`hold_turn`), which keeps the workflow inside
    that turn and so inside one run. `run_ids` lists the runs that called an activity, in the
    order they began, and `turn_runs` says which run made the model call of each turn. A
    `bundle_checkpoint` or `record_event` that has an error in `fails` raises it, and so does
    any of the GitHub activities.

    The GitHub activities of LH-306 (acts.GITHUB) are logged in `github_log` and in `every`, the
    order of all activities, and not in `log`. `names()` therefore lists the loop's activities
    only, and `github_names()` the others.
    """

    MUTATING = frozenset({"edit_file", "write_file"})

    def __init__(self, script: Script | None = None, *, cost_usd: float = 0.0) -> None:
        self.script: Script = script or (lambda turn: [finish()])
        self.cost_usd = cost_usd
        self.log: list[tuple[str, Any]] = []
        self.github_log: list[tuple[str, Any]] = []
        self.every: list[str] = []
        self.gates = {act.name: asyncio.Event() for act in (*acts.ALL, *acts.GITHUB)}
        for gate in self.gates.values():
            gate.set()
        self.commits = 0
        self.gone: dict[str, int] = {}
        self.recreated = 0
        self.turn_gates: dict[int, asyncio.Event] = {}
        self.run_ids: list[str] = []
        self.workflow_ids: set[str] = set()
        self.turn_runs: dict[int, str] = {}
        self.fails: dict[str, ApplicationError] = {}
        self.decisions: dict[int, Decision] = {}
        self.overflow: dict[int, int] = {}
        self.compaction: Callable[[acts.CompactInput], acts.CompactOutput] = compacted

    @property
    def activities(self) -> list[Callable[..., Any]]:
        return [
            self.prepare_workspace,
            self.ensure_sandbox,
            self.llm_turn,
            self.tool_exec,
            self.append_results,
            self.cleanup,
            self.bundle_checkpoint,
            self.record_event,
            self.compact,
            self.post_comment,
            self.add_label,
            self.remove_label,
            self.build_pr_branch,
            self.deliver,
        ]

    def hold(self, name: str) -> None:
        self.gates[name].clear()

    def release(self, name: str) -> None:
        self.gates[name].set()

    def hold_turn(self, turn: int) -> None:
        self.turn_gates.setdefault(turn, asyncio.Event()).clear()

    def release_turn(self, turn: int) -> None:
        self.turn_gates.setdefault(turn, asyncio.Event()).set()

    def started(self, turn: int) -> bool:
        return any(p.turn_index == turn for p in self.payloads(acts.LLM_TURN.name))

    def names(self) -> list[str]:
        return [name for name, _ in self.log]

    def github_names(self) -> list[str]:
        return [name for name, _ in self.github_log]

    def payloads(self, name: str) -> list[Any]:
        return [payload for logged, payload in (*self.log, *self.github_log) if logged == name]

    async def _enter(self, name: str, payload: Any, *, gate: asyncio.Event | None = None) -> None:
        info = activity.info()
        self.workflow_ids.add(info.workflow_id)
        if info.workflow_run_id not in self.run_ids:
            self.run_ids.append(info.workflow_run_id)
        (self.github_log if name in GITHUB_NAMES else self.log).append((name, payload))
        self.every.append(name)
        try:
            while not (self.gates[name].is_set() and (gate is None or gate.is_set())):
                if not info.is_local:  # a local activity has no heartbeat
                    activity.heartbeat()
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            # The workflow cancelled this activity, or the worker is shutting down.
            (self.github_log if name in GITHUB_NAMES else self.log).append(
                (f"{name}:cancelled", None)
            )
            self.every.append(f"{name}:cancelled")
            raise

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        await self._enter(acts.PREPARE_WORKSPACE.name, spec)
        return acts.PrepareWorkspaceOutput(sandbox_id="sbx-1", base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        self.turn_runs[inp.turn_index] = activity.info().workflow_run_id
        await self._enter(acts.LLM_TURN.name, inp, gate=self.turn_gates.get(inp.turn_index))
        if self.overflow.get(inp.turn_index, 0) > 0:
            self.overflow[inp.turn_index] -= 1
            raise ApplicationError(
                "prompt is too long", type="context_too_long", non_retryable=True
            )
        return acts.LlmTurnOutput(
            transcript_ref=ref(f"turns/{inp.turn_index:04d}/assistant.json"),
            tool_call_summaries=self.script(inp.turn_index),
            stop_reason="tool_use",
            usage=Usage(),
            cost_usd=self.cost_usd,
            context_decision=self.decisions.get(inp.turn_index, "ok"),
        )

    @activity.defn(name=acts.ENSURE_SANDBOX.name)
    async def ensure_sandbox(self, inp: acts.EnsureSandboxInput) -> str:
        await self._enter(acts.ENSURE_SANDBOX.name, inp)
        self.recreated += 1
        return f"sbx-{self.recreated + 1}"

    @activity.defn(name=acts.TOOL_EXEC.name)
    async def tool_exec(self, inp: acts.ToolExecInput) -> ToolResult:
        await self._enter(acts.TOOL_EXEC.name, inp)
        if self.gone.get(inp.call.id, 0) > 0:
            self.gone[inp.call.id] -= 1
            # What the real activity raises when the container is not there.
            raise ApplicationError(
                f"sandbox {inp.sandbox_id} is gone", type=acts.SANDBOX_GONE, non_retryable=True
            )
        post_sha = None
        if inp.call.name in self.MUTATING:
            self.commits += 1
            post_sha = sha(self.commits)
        return ToolResult(
            call_id=inp.call.id,
            ok=True,
            output=f"ran {inp.call.name}",
            pre_sha=inp.pre_sha,
            post_sha=post_sha,
        )

    @activity.defn(name=acts.APPEND_RESULTS.name)
    async def append_results(self, inp: acts.AppendResultsInput) -> BlobRef:
        await self._enter(acts.APPEND_RESULTS.name, inp)
        return ref(f"turns/{inp.turn_index:04d}/results.json")

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        await self._enter(acts.CLEANUP.name, inp)

    @activity.defn(name=acts.BUNDLE_CHECKPOINT.name)
    async def bundle_checkpoint(self, inp: acts.BundleCheckpointInput) -> BlobRef:
        await self._enter(acts.BUNDLE_CHECKPOINT.name, inp)
        if acts.BUNDLE_CHECKPOINT.name in self.fails:
            raise self.fails[acts.BUNDLE_CHECKPOINT.name]
        return ref(f"ckpt/{inp.turn_index}.bundle")

    @activity.defn(name=acts.RECORD_EVENT.name)
    async def record_event(self, inp: acts.RecordEventInput) -> None:
        await self._enter(acts.RECORD_EVENT.name, inp)
        if acts.RECORD_EVENT.name in self.fails:
            raise self.fails[acts.RECORD_EVENT.name]

    @activity.defn(name=acts.COMPACT.name)
    async def compact(self, inp: acts.CompactInput) -> acts.CompactOutput:
        await self._enter(acts.COMPACT.name, inp)
        if acts.COMPACT.name in self.fails:
            raise self.fails[acts.COMPACT.name]
        return self.compaction(inp)

    async def _github(self, name: str, payload: Any) -> None:
        await self._enter(name, payload)
        if name in self.fails:
            raise self.fails[name]

    @activity.defn(name=acts.POST_COMMENT.name)
    async def post_comment(self, inp: CommentInput) -> str:
        await self._github(acts.POST_COMMENT.name, inp)
        return "comment-1"

    @activity.defn(name=acts.ADD_LABEL.name)
    async def add_label(self, inp: LabelInput) -> str:
        await self._github(acts.ADD_LABEL.name, inp)
        return inp.label

    @activity.defn(name=acts.REMOVE_LABEL.name)
    async def remove_label(self, inp: LabelInput) -> str:
        await self._github(acts.REMOVE_LABEL.name, inp)
        return inp.label

    @activity.defn(name=acts.BUILD_PR_BRANCH.name)
    async def build_pr_branch(self, inp: acts.BuildPrBranchInput) -> acts.PrBranchOutput:
        await self._github(acts.BUILD_PR_BRANCH.name, inp)
        return acts.PrBranchOutput(
            branch="longhaul-pr/42",
            head_sha=sha(900),
            base_sha=BASE,
            base_branch="main",
            title="fix(calc): fix add",
        )

    @activity.defn(name=acts.DELIVER.name)
    async def deliver(self, inp: acts.DeliverInput) -> str:
        await self._github(acts.DELIVER.name, inp)
        return PR_URL


NOTES = ProgressNotes(
    goal="fix add()",
    plan_status=[],
    files_touched=["calc.py"],
    decisions=[],
    failing_tests=[],
    open_hypotheses=[],
    tried_and_failed=["swapping the operands"],
    covers_turns_up_to=1,
)


def compacted(inp: acts.CompactInput) -> acts.CompactOutput:
    """The default compaction: a new view named for the turn, and the notes above."""
    return acts.CompactOutput(
        transcript_ref=ref(f"turns/{inp.turn_index:04d}/compacted.json"),
        notes=NOTES,
        status="ok",
        tokens_before=5_000,
        tokens_after=2_000,
        folded_turns=4,
        cost_usd=0.25,
    )


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def make_spec(
    task_id: str = "lh-acme-calc-42",
    *,
    task_turns: int | None = None,
    repo: str = "acme/calc",
    issue: int | None = None,
) -> TaskSpec:
    """A task on an owner/name repo with no issue, which is not delivered as a pull request."""
    budgets = Budgets() if task_turns is None else Budgets(task_turns=task_turns)
    return TaskSpec(
        task_id=task_id,
        repo=repo,
        issue_number=issue,
        title="add() subtracts",
        body="add(2, 3) returns -1",
        settings=RepoSettings(repo=repo, budgets=budgets),
    )


def github_spec(task_id: str = "lh-acme-calc-42", *, task_turns: int | None = None) -> TaskSpec:
    """The task of an issue: an owner/name repo and an issue number."""
    return make_spec(task_id, task_turns=task_turns, issue=42)


@pytest.fixture
def spec() -> TaskSpec:
    return make_spec()


@pytest.fixture
def gh_spec() -> TaskSpec:
    return github_spec()


def initial_state(spec: TaskSpec) -> CarryState:
    """What start_task builds, for the tests that start the workflow with extra options."""
    return CarryState(spec=spec, phase=Phase.QUEUED, started_at=datetime.now(UTC))


async def start_cancelled(
    env: WorkflowEnvironment, queue: str, spec: TaskSpec, reason: str = "too late"
) -> WorkflowHandle[Any, Outcome]:
    """Start a task whose cancel signal rides on the start request, so no activity is running."""
    return await env.client.start_workflow(
        TaskWorkflow.run,
        initial_state(spec),
        id=spec.task_id,
        task_queue=queue,
        start_signal="cancel",
        start_signal_args=["alice", reason],
    )


@asynccontextmanager
async def running(
    env: WorkflowEnvironment, stubs: Stubs, workflows: Sequence[type] = (TaskWorkflow,)
) -> AsyncIterator[str]:
    """A worker for the workflows (TaskWorkflow) and the stubs. Yields the task queue it polls."""
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=workflows,
        activities=stubs.activities,
        workflow_runner=workflow_runner(),
    ):
        yield queue


async def wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def wait_for_phase(handle: WorkflowHandle[Any, Outcome], phase: Phase) -> TaskStatus:
    """Poll status() until the workflow reports the phase. Time only moves while we wait."""
    deadline = time.monotonic() + WAIT_S
    while True:
        status = await handle.query(TaskWorkflow.status)
        if status.phase is phase:
            return status
        if time.monotonic() > deadline:
            raise AssertionError(f"workflow stayed in {status.phase!r}, wanted {phase!r}")
        await asyncio.sleep(0.02)


async def result_of(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    return await asyncio.wait_for(handle.result(), WAIT_S)


# ---------- the acceptance criteria of LH-203 ----------
async def test_status_query_phases(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Stubs()
    stubs.hold(acts.PREPARE_WORKSPACE.name)
    stubs.hold(acts.LLM_TURN.name)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)

        seen = [(await wait_for_phase(handle, Phase.PREPARING)).phase]
        assert (await handle.query(TaskWorkflow.status)).last_checkpoint is None
        stubs.release(acts.PREPARE_WORKSPACE.name)

        coding = await wait_for_phase(handle, Phase.CODING)
        seen.append(coding.phase)
        assert coding.last_checkpoint == BASE
        assert coding.turn_index == 0
        assert coding.plan_version is None
        assert coding.outcome is None
        stubs.release(acts.LLM_TURN.name)

        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert seen == [Phase.PREPARING, Phase.CODING]
    assert final.phase is Phase.DONE
    assert final.outcome == outcome


async def test_cancel_signal(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Stubs()
    stubs.hold(acts.LLM_TURN.name)  # never opened: the cancel has to end the run
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=SHORT_HEARTBEAT)
        await wait_for_phase(handle, Phase.CODING)
        await wait_until(lambda: acts.LLM_TURN.name in stubs.names(), "the turn to start")

        await handle.signal("cancel", args=["alice", "changed my mind"])
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(
        kind=OutcomeKind.CANCELLED, summary="cancelled by alice: changed my mind"
    )
    assert stubs.payloads(acts.CLEANUP.name) == [
        acts.CleanupInput(task_id=spec.task_id, sandbox_id="sbx-1", reason="changed my mind")
    ]
    assert final.phase is Phase.CANCELLED
    assert final.outcome == outcome
    # Cleanup removes the sandbox, so it must not start while the cancelled turn still runs.
    assert stubs.names() == ["prepare_workspace", "llm_turn", "llm_turn:cancelled", "cleanup"]


async def test_cancel_during_prepare_cleans_up_without_a_sandbox_id(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.hold(acts.PREPARE_WORKSPACE.name)  # never opened
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=SHORT_HEARTBEAT)
        await wait_for_phase(handle, Phase.PREPARING)
        await wait_until(lambda: acts.PREPARE_WORKSPACE.name in stubs.names(), "prepare to start")

        await handle.signal("cancel", args=["console", "wrong repo"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.CANCELLED
    # The workspace may exist without the workflow knowing its id; cleanup finds it by task.
    assert stubs.payloads(acts.CLEANUP.name) == [
        acts.CleanupInput(task_id=spec.task_id, sandbox_id=None, reason="wrong repo")
    ]
    # Cleanup waits for prepare to stop, so it cannot miss a sandbox that prepare was creating.
    assert stubs.names() == ["prepare_workspace", "prepare_workspace:cancelled", "cleanup"]


async def test_cancel_before_the_first_activity_starts_nothing(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # The signal rides on the start request, so the handler runs before run() does.
    stubs = Stubs()
    async with running(env, stubs) as queue:
        outcome = await result_of(await start_cancelled(env, queue, spec))

    assert outcome == Outcome(kind=OutcomeKind.CANCELLED, summary="cancelled by alice: too late")
    assert stubs.names() == [acts.CLEANUP.name]


async def test_cleanup_that_keeps_failing_does_not_stop_a_cancel(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    class BrokenCleanup(Stubs):
        @activity.defn(name=acts.CLEANUP.name)
        async def cleanup(self, inp: acts.CleanupInput) -> None:
            await self._enter(acts.CLEANUP.name, inp)
            raise RuntimeError("docker daemon is gone")

    stubs = BrokenCleanup()
    async with running(env, stubs) as queue:
        outcome = await result_of(await start_cancelled(env, queue, spec))

    assert outcome.kind is OutcomeKind.CANCELLED
    assert len(stubs.payloads(acts.CLEANUP.name)) == acts.CLEANUP.retry.maximum_attempts


async def test_duplicate_start_rejected(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Stubs()
    stubs.hold(acts.PREPARE_WORKSPACE.name)
    async with running(env, stubs) as queue:
        first = await start_task(env.client, spec, task_queue=queue)

        with pytest.raises(WorkflowAlreadyStartedError) as caught:
            await start_task(env.client, spec, task_queue=queue)

        # The error names the task and the run that holds the id.
        assert caught.value.workflow_id == spec.task_id
        assert caught.value.run_id == first.result_run_id
        # The first run is untouched.
        assert (await first.query(TaskWorkflow.status)).phase is Phase.PREPARING
        stubs.release(acts.PREPARE_WORKSPACE.name)
        assert (await result_of(first)).kind is OutcomeKind.PR_OPENED


async def test_duplicate_start_rejected_after_the_first_run_finished(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # The SDK default would start a new run here, and rerun a finished task.
    stubs = Stubs()
    async with running(env, stubs) as queue:
        first = await start_task(env.client, spec, task_queue=queue)
        await result_of(first)

        with pytest.raises(WorkflowAlreadyStartedError):
            await start_task(env.client, spec, task_queue=queue)

    assert stubs.names().count(acts.PREPARE_WORKSPACE.name) == 1


# ---------- the loop ----------
async def test_tool_calls_run_in_order_and_each_starts_from_the_last_checkpoint(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    def script(turn: int) -> list[acts.ToolCallSummary]:
        if turn == 0:
            return [
                call("read_file", "c1", path="calc.py"),
                call("edit_file", "c2", path="calc.py"),
                call("run_tests", "c3"),
            ]
        return [finish()]

    stubs = Stubs(script)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert stubs.names() == [
        "prepare_workspace",
        "llm_turn",
        "tool_exec",
        "tool_exec",
        "tool_exec",
        "append_results",
        "llm_turn",
        "append_results",  # the finish still gets its answer, so the transcript stays paired
    ]
    tools = stubs.payloads(acts.TOOL_EXEC.name)
    assert [t.call.id for t in tools] == ["c1", "c2", "c3"]
    # c2 edits the workspace and gets a checkpoint, and c3 starts from it.
    assert [t.pre_sha for t in tools] == [BASE, BASE, sha(1)]
    assert {(t.task_id, t.turn_index, t.sandbox_id) for t in tools} == {(spec.task_id, 0, "sbx-1")}

    appended, finish_answer = stubs.payloads(acts.APPEND_RESULTS.name)
    assert appended.transcript_ref == ref("turns/0000/assistant.json")
    assert [r.call_id for r in appended.results] == ["c1", "c2", "c3"]
    assert [r.output for r in appended.results] == [
        "ran read_file",
        "ran edit_file",
        "ran run_tests",
    ]
    # Only the calls that ran have a result. finish gets its answer from append_results itself.
    assert finish_answer.transcript_ref == ref("turns/0001/assistant.json")
    assert finish_answer.results == []

    # The next turn reads the transcript that append_results wrote.
    first_turn, second_turn = stubs.payloads(acts.LLM_TURN.name)
    assert (first_turn.turn_index, first_turn.transcript_ref) == (0, None)
    assert (second_turn.turn_index, second_turn.transcript_ref) == (
        1,
        ref("turns/0000/results.json"),
    )
    assert first_turn.task_id == spec.task_id

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="fixed add()")
    assert (final.phase, final.turn_index, final.last_checkpoint) == (Phase.DONE, 2, sha(1))


async def test_finish_ends_the_run_and_skips_the_calls_after_it(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(
        lambda turn: [call("edit_file", "c1"), finish("all done"), call("edit_file", "c2")]
    )
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="all done")
    # Nothing runs for finish itself or for the call after it. The results are still appended,
    # so finish gets its answer and c2 is marked as not run.
    assert stubs.names() == ["prepare_workspace", "llm_turn", "tool_exec", "append_results"]
    assert [t.call.id for t in stubs.payloads(acts.TOOL_EXEC.name)] == ["c1"]
    (appended,) = stubs.payloads(acts.APPEND_RESULTS.name)
    assert [r.call_id for r in appended.results] == ["c1"]
    assert (final.phase, final.last_checkpoint) == (Phase.DONE, sha(1))


async def test_a_hint_reaches_the_next_turn(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Stubs(lambda turn: never_finishes(turn) if turn == 0 else [finish()])
    stubs.hold(acts.LLM_TURN.name)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        await wait_until(lambda: acts.LLM_TURN.name in stubs.names(), "the first turn")

        await handle.signal("hint", args=["alice", "use pytest -x"])
        stubs.release(acts.LLM_TURN.name)
        await result_of(handle)

    first, second = stubs.payloads(acts.LLM_TURN.name)
    # The first turn was already running when the hint arrived.
    assert first.hints == []
    assert second.hints == ["alice: use pytest -x"]


async def test_a_signal_sent_with_the_start_is_not_lost(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # The signal reaches the workflow in its first task, before run() has done anything. The
    # state is built in @workflow.init for this reason: a handler that runs first would find
    # nothing to write to, or write to state that run() then replaces (ADR-0004 section 4).
    stubs = Stubs()
    async with running(env, stubs) as queue:
        handle = await env.client.start_workflow(
            TaskWorkflow.run,
            initial_state(spec),
            id=spec.task_id,
            task_queue=queue,
            start_signal="hint",
            start_signal_args=["alice", "use pytest -x"],
        )
        await result_of(handle)

    (first,) = stubs.payloads(acts.LLM_TURN.name)
    assert first.hints == ["alice: use pytest -x"]


async def test_the_turn_limit_ends_the_run_with_an_error(env: WorkflowEnvironment) -> None:
    stubs = Stubs(never_finishes)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, make_spec(task_turns=2), task_queue=queue)
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="turn limit")
    assert len(stubs.payloads(acts.LLM_TURN.name)) == 2
    assert (final.phase, final.turn_index) == (Phase.FAILED, 2)


async def test_add_budget_turns_raise_the_turn_limit(env: WorkflowEnvironment) -> None:
    stubs = Stubs(never_finishes)
    stubs.hold(acts.LLM_TURN.name)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, make_spec(task_turns=2), task_queue=queue)
        await wait_until(lambda: acts.LLM_TURN.name in stubs.names(), "the first turn")

        await handle.signal("add_budget", args=["alice", 1.0, 1])
        stubs.release(acts.LLM_TURN.name)
        outcome = await result_of(handle)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="turn limit")
    assert len(stubs.payloads(acts.LLM_TURN.name)) == 3


async def test_a_turn_without_tool_calls_gets_a_nudge_and_a_second_one_ends_the_run(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(lambda turn: [])
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="no tool calls")
    # One nudge, not two: the second idle turn is the last, and nothing is appended after it.
    assert stubs.names() == ["prepare_workspace", "llm_turn", "append_results", "llm_turn"]
    (nudge,) = stubs.payloads(acts.APPEND_RESULTS.name)
    assert (nudge.results, nudge.user_text) == ([], NUDGE)
    assert nudge.transcript_ref == ref("turns/0000/assistant.json")
    assert (final.phase, final.turn_index) == (Phase.FAILED, 2)


async def test_idle_turns_that_are_not_in_a_row_do_not_end_the_run(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    def script(turn: int) -> list[acts.ToolCallSummary]:
        return [] if turn in (0, 2) else [finish()] if turn == 4 else never_finishes(turn)

    stubs = Stubs(script)
    async with running(env, stubs) as queue:
        outcome = await result_of(await start_task(env.client, spec, task_queue=queue))

    assert outcome.kind is OutcomeKind.PR_OPENED
    nudges = [p.user_text for p in stubs.payloads(acts.APPEND_RESULTS.name) if p.user_text]
    assert nudges == [NUDGE, NUDGE]


async def test_the_cost_of_each_turn_adds_up(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Stubs(lambda turn: never_finishes(turn) if turn == 0 else [finish()], cost_usd=0.25)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert final.cost_usd == pytest.approx(0.5)


# ---------- the stop reasons (D8 and D9) ----------
Reply = acts.LlmTurnOutput | ApplicationError


def out(
    name: str,
    stop: StopReason = "tool_use",
    calls: list[acts.ToolCallSummary] | None = None,
    *,
    cost: float = 0.0,
    **fields: Any,
) -> acts.LlmTurnOutput:
    """A model turn as the llm_turn activity returns it. `name` makes its transcript ref."""
    return acts.LlmTurnOutput(
        transcript_ref=ref(f"{name}.json"),
        tool_call_summaries=calls or [],
        stop_reason=stop,
        usage=Usage(),
        cost_usd=cost,
        **fields,
    )


class Replying(Stubs):
    """Stubs whose model turns answer from a list of replies, or fail with a given error."""

    def __init__(self, *replies: Reply) -> None:
        super().__init__()
        self.replies = list(replies)

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        await self._enter(acts.LLM_TURN.name, inp)
        reply = self.replies.pop(0)
        if isinstance(reply, ApplicationError):
            raise reply
        return reply


async def play(
    env: WorkflowEnvironment, stubs: Stubs, spec: TaskSpec
) -> tuple[Outcome, TaskStatus]:
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        outcome = await result_of(handle)
        return outcome, await handle.query(TaskWorkflow.status)


async def test_a_turn_cut_off_at_max_tokens_is_asked_again_once_at_the_higher_cap(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    cut = out("cut", "max_tokens", [call("write_file", "w1", path="big.py")], cost=0.1)
    stubs = Replying(cut, out("done", "tool_use", [finish()], cost=0.2))
    outcome, final = await play(env, stubs, spec)

    first, second = stubs.payloads(acts.LLM_TURN.name)
    assert (first.high_output_cap, second.high_output_cap) == (False, True)
    # It is the same turn, and it carries on from the transcript the first call handed back.
    assert first.turn_index == second.turn_index == 0
    assert second.transcript_ref == ref("cut.json")
    # The cut reply's call did not run. Both calls cost money.
    assert acts.TOOL_EXEC.name not in stubs.names()
    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="fixed add()")
    assert final.cost_usd == pytest.approx(0.3)
    assert final.turn_index == 1


async def test_a_second_max_tokens_stop_ends_the_run_with_output_limit(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Replying(out("cut1", "max_tokens"), out("cut2", "max_tokens"))
    outcome, final = await play(env, stubs, spec)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="output limit")
    assert stubs.names() == ["prepare_workspace", "llm_turn", "llm_turn"]
    assert (final.phase, final.turn_index) == (Phase.FAILED, 1)


async def test_the_retry_is_per_turn(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Replying(
        out("a", "max_tokens"),
        out("b", "tool_use", never_finishes(0)),
        out("c", "max_tokens"),
        out("d", "tool_use", [finish()]),
    )
    outcome, _ = await play(env, stubs, spec)

    assert outcome.kind is OutcomeKind.PR_OPENED
    caps = [i.high_output_cap for i in stubs.payloads(acts.LLM_TURN.name)]
    assert caps == [False, True, False, True]


async def test_a_refusal_ends_the_run_and_runs_nothing(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    details = StopDetails(category="cyber", explanation="The request looks like malware.")
    refusal = out("r", "refusal", never_finishes(0), stop_details=details)
    stubs = Replying(refusal)
    outcome, final = await play(env, stubs, spec)

    assert outcome == Outcome(
        kind=OutcomeKind.ERROR,
        summary="model declined (cyber): The request looks like malware.",
    )
    assert stubs.names() == ["prepare_workspace", "llm_turn"]
    assert (final.phase, final.turn_index) == (Phase.FAILED, 1)


@pytest.mark.parametrize(
    ("stop", "raw", "summary"),
    [
        ("model_context_window_exceeded", None, "context limit"),
        ("stop_sequence", None, "unexpected stop reason: stop_sequence"),
        ("other", "odd_reason", "unexpected stop reason: odd_reason"),
    ],
)
async def test_the_other_stops_end_the_run(
    env: WorkflowEnvironment, spec: TaskSpec, stop: StopReason, raw: str | None, summary: str
) -> None:
    stubs = Replying(out("x", stop, never_finishes(0), raw_stop_reason=raw))
    outcome, _ = await play(env, stubs, spec)
    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary=summary)
    assert stubs.names() == ["prepare_workspace", "llm_turn"]


async def test_a_paused_turn_is_continued_with_the_text_it_left(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    paused = out("p", "pause_turn", never_finishes(0), paused_ref=ref("paused.json"))
    stubs = Replying(paused, out("done", "tool_use", [finish()]))
    outcome, final = await play(env, stubs, spec)

    first, second = stubs.payloads(acts.LLM_TURN.name)
    assert (first.paused_ref, second.paused_ref) == (None, ref("paused.json"))
    assert second.transcript_ref == ref("p.json")
    # The call that came with the pause was not run, and the pause did not count as a turn.
    assert acts.TOOL_EXEC.name not in stubs.names()
    assert outcome.kind is OutcomeKind.PR_OPENED
    assert final.turn_index == 1


async def test_a_pause_does_not_carry_into_the_next_turn(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Replying(
        out("p", "pause_turn", paused_ref=ref("paused.json")),
        out("a", "tool_use", never_finishes(0)),
        out("b", "tool_use", [finish()]),
    )
    await play(env, stubs, spec)
    assert [i.paused_ref for i in stubs.payloads(acts.LLM_TURN.name)] == [
        None,
        ref("paused.json"),
        None,
    ]


async def test_pauses_have_a_limit(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Replying(*[out(f"p{n}", "pause_turn") for n in range(MAX_PAUSES + 1)])
    outcome, _ = await play(env, stubs, spec)
    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="pause limit")
    assert len(stubs.payloads(acts.LLM_TURN.name)) == MAX_PAUSES + 1


async def test_a_prompt_over_the_window_ends_the_run(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    too_long = ApplicationError("prompt is too long", type="context_too_long", non_retryable=True)
    stubs = Replying(too_long)
    outcome, final = await play(env, stubs, spec)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="context limit")
    assert (final.phase, final.turn_index) == (Phase.FAILED, 1)


async def test_a_failure_that_is_not_the_runs_to_end_fails_the_workflow(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    refused = ApplicationError("bad request", type="invalid_request", non_retryable=True)
    async with running(env, Replying(refused)) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)


async def test_a_finish_without_a_summary_is_answered_and_the_task_goes_on(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(lambda turn: [call("finish", "f1")] if turn == 0 else [finish("Done properly.")])
    outcome, _ = await play(env, stubs, spec)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Done properly.")
    # Nothing runs for finish. The first one gets an error that tells the model what to fix.
    assert acts.TOOL_EXEC.name not in stubs.names()
    first, second = stubs.payloads(acts.APPEND_RESULTS.name)
    assert first.results == [acts.ToolResultSummary(call_id="f1", ok=False, output=BAD_FINISH)]
    assert second.results == []


# ---------- what reaches Temporal ----------
def scheduled_options(history: WorkflowHistory) -> dict[str, Any]:
    """The options the workflow gave each kind of activity, read from its history."""
    scheduled = {}
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            attributes = event.activity_task_scheduled_event_attributes
            scheduled[attributes.activity_type.name] = attributes
    return scheduled


def timeouts(attributes: Any) -> tuple[float, float | None]:
    """(start-to-close, heartbeat) in seconds. The heartbeat is None when none was set.

    The test server writes a zero duration for a timeout that was not set, so zero means none.
    """
    heartbeat = attributes.heartbeat_timeout.ToTimedelta().total_seconds()
    return attributes.start_to_close_timeout.ToTimedelta().total_seconds(), heartbeat or None


async def test_activities_get_the_timeouts_and_retries_of_section_3_7(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(lambda turn: never_finishes(turn) if turn == 0 else [finish()])
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        await result_of(handle)
        sched = scheduled_options(await handle.fetch_history())

    assert {name: timeouts(attributes) for name, attributes in sched.items()} == {
        "prepare_workspace": (15 * 60, 60),
        "llm_turn": (10 * 60, 30),
        "tool_exec": (660, 15),
        "append_results": (2 * 60, None),
    }
    attempts = {
        name: attributes.retry_policy.maximum_attempts for name, attributes in sched.items()
    }
    assert attempts == {
        "prepare_workspace": 3,
        "llm_turn": 8,
        "tool_exec": 3,
        "append_results": 3,
    }
    llm = sched["llm_turn"].retry_policy
    assert llm.initial_interval.ToTimedelta() == timedelta(seconds=2)
    assert llm.maximum_interval.ToTimedelta() == timedelta(seconds=120)
    assert llm.backoff_coefficient == 2.0
    assert {"invalid_request", "auth", "context_too_long"} <= set(llm.non_retryable_error_types)


async def test_cleanup_gets_its_own_timeout_and_retries(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs()
    async with running(env, stubs) as queue:
        handle = await start_cancelled(env, queue, spec)
        await result_of(handle)
        sched = scheduled_options(await handle.fetch_history())

    assert timeouts(sched["cleanup"]) == (2 * 60, None)
    assert sched["cleanup"].retry_policy.maximum_attempts == 3


async def test_tuning_shortens_the_heartbeat_timeouts_of_one_run(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(lambda turn: never_finishes(turn) if turn == 0 else [finish()])
    async with running(env, stubs) as queue:
        handle = await start_task(
            env.client, spec, task_queue=queue, tuning=acts.Tuning(heartbeat_timeout_s=2)
        )
        await result_of(handle)
        sched = scheduled_options(await handle.fetch_history())

    # Only the heartbeat timeouts change, and an activity without one stays without.
    assert {name: timeouts(attributes) for name, attributes in sched.items()} == {
        "prepare_workspace": (15 * 60, 2),
        "llm_turn": (10 * 60, 2),
        "tool_exec": (660, 2),
        "append_results": (2 * 60, None),
    }


# ---------- a script that travels with the task (LH-208) ----------
async def test_the_script_in_the_memo_goes_to_every_model_turn(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    script = ref("script.yaml")
    stubs = Stubs(lambda turn: never_finishes(turn) if turn < 2 else [finish()])
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, script=script)
        await result_of(handle)

    turns = stubs.payloads(acts.LLM_TURN.name)
    assert len(turns) == 3
    assert [turn.script_ref for turn in turns] == [script] * 3


async def test_a_task_without_a_script_sends_none_to_the_model_turns(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs()
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        await result_of(handle)

    assert [turn.script_ref for turn in stubs.payloads(acts.LLM_TURN.name)] == [None]


async def test_the_script_and_the_tuning_can_ride_in_the_same_memo(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    script = ref("script.yaml")
    stubs = Stubs()
    async with running(env, stubs) as queue:
        handle = await start_task(
            env.client, spec, task_queue=queue, tuning=SHORT_HEARTBEAT, script=script
        )
        await result_of(handle)
        sched = scheduled_options(await handle.fetch_history())

    assert [turn.script_ref for turn in stubs.payloads(acts.LLM_TURN.name)] == [script]
    assert timeouts(sched["llm_turn"]) == (10 * 60, 2)


# ---------- sandbox recovery (LH-206) ----------
def resumed(spec: TaskSpec) -> CarryState:
    """What a run starts from when an earlier run already prepared a sandbox."""
    return initial_state(spec).model_copy(
        update={"phase": Phase.CODING, "sandbox_id": "sbx-1", "head_sha": sha(7)}
    )


async def start_with(
    env: WorkflowEnvironment, queue: str, state: CarryState, *, tuning: acts.Tuning | None = None
) -> WorkflowHandle[Any, Outcome]:
    return await env.client.start_workflow(
        TaskWorkflow.run,
        state,
        id=state.spec.task_id,
        task_queue=queue,
        memo={acts.TUNING_MEMO_KEY: tuning} if tuning is not None else None,
    )


async def test_a_sandbox_that_is_gone_is_recreated_and_the_call_runs_again_from_the_same_sha(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    def script(turn: int) -> list[acts.ToolCallSummary]:
        if turn == 0:
            return [call("edit_file", "c1"), call("run_tests", "c2")]
        return [finish()]

    stubs = Stubs(script)
    stubs.gone["c2"] = 1
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert stubs.names() == [
        "prepare_workspace",
        "llm_turn",
        "tool_exec",  # c1 edits and leaves the checkpoint sha(1)
        "tool_exec",  # c2 finds the sandbox gone
        "ensure_sandbox",
        "tool_exec",  # c2 again, in the new sandbox
        "append_results",
        "llm_turn",
        "append_results",
    ]
    # The sandbox is rebuilt at the checkpoint of the last call that changed the workspace.
    assert stubs.payloads(acts.ENSURE_SANDBOX.name) == [
        acts.EnsureSandboxInput(
            task_id=spec.task_id, head_sha=sha(1), sandbox_id="sbx-1", spec=spec
        )
    ]
    first, gone, again = stubs.payloads(acts.TOOL_EXEC.name)
    assert [(t.call.id, t.pre_sha, t.sandbox_id) for t in (first, gone, again)] == [
        ("c1", BASE, "sbx-1"),
        ("c2", sha(1), "sbx-1"),
        ("c2", sha(1), "sbx-2"),
    ]
    # The retry is not the activity's: it failed for good, and the workflow chose to go on.
    appended, _ = stubs.payloads(acts.APPEND_RESULTS.name)
    assert [r.call_id for r in appended.results] == ["c1", "c2"]
    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="fixed add()")
    assert final.last_checkpoint == sha(1)


async def test_every_call_after_a_recreation_runs_in_the_new_sandbox(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    def script(turn: int) -> list[acts.ToolCallSummary]:
        if turn == 0:
            return [call("read_file", "c1"), call("read_file", "c2")]
        return [call("read_file", "c3")] if turn == 1 else [finish()]

    stubs = Stubs(script)
    stubs.gone["c1"] = 1
    outcome, _ = await play(env, stubs, spec)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert [(t.call.id, t.sandbox_id) for t in stubs.payloads(acts.TOOL_EXEC.name)] == [
        ("c1", "sbx-1"),
        ("c1", "sbx-2"),
        ("c2", "sbx-2"),
        ("c3", "sbx-2"),
    ]
    assert len(stubs.payloads(acts.ENSURE_SANDBOX.name)) == 1


async def test_a_sandbox_that_keeps_dying_ends_the_run_with_an_error(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(lambda turn: [call("run_tests", "c1")])
    stubs.gone["c1"] = 99
    outcome, final = await play(env, stubs, spec)

    # The call is tried once and again after each of the two recreations, then the run ends.
    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="sandbox lost")
    assert stubs.names() == [
        "prepare_workspace",
        "llm_turn",
        "tool_exec",
        "ensure_sandbox",
        "tool_exec",
        "ensure_sandbox",
        "tool_exec",
        # The run ends here, and the transcript still answers the call.
        "append_results",
    ]
    assert acts.MAX_SANDBOX_RECREATIONS == 2
    assert (final.phase, final.turn_index) == (Phase.FAILED, 1)
    assert final.outcome == outcome


async def test_a_run_lost_to_a_dying_sandbox_still_answers_every_call_of_the_turn(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # The transcript of a run that ends must stay paired: a later report, resume or retry sends
    # it to the model again, and the API answers 400 to a tool_use with no tool_result.
    def script(turn: int) -> list[acts.ToolCallSummary]:
        return [call("read_file", "c1"), call("run_tests", "c2"), finish()]

    stubs = Stubs(script)
    stubs.gone["c2"] = 99
    outcome, final = await play(env, stubs, spec)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="sandbox lost")
    (appended,) = stubs.payloads(acts.APPEND_RESULTS.name)
    assert appended.turn_index == 0
    assert appended.transcript_ref == ref("turns/0000/assistant.json")
    # One result for each call, in the order of the calls. c1 ran, c2 lost its sandbox for good,
    # and the finish after it never ran.
    by_id = {r.call_id: r for r in appended.results}
    assert list(by_id) == ["c1", "c2", "finish-1"]
    assert (by_id["c1"].ok, by_id["c1"].output) == (True, "ran read_file")
    for lost in ("c2", "finish-1"):
        assert by_id[lost].ok is False
        assert "sandbox" in (by_id[lost].output or "")
    assert (final.phase, final.turn_index) == (Phase.FAILED, 1)


async def test_the_limit_on_recreations_counts_each_call_on_its_own(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(
        lambda turn: [call("read_file", "c1"), call("read_file", "c2")] if turn == 0 else [finish()]
    )
    stubs.gone.update({"c1": 2, "c2": 2})
    outcome, _ = await play(env, stubs, spec)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert len(stubs.payloads(acts.ENSURE_SANDBOX.name)) == 4


async def test_a_tool_failure_that_is_not_a_lost_sandbox_fails_the_workflow(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    class Failing(Stubs):
        @activity.defn(name=acts.TOOL_EXEC.name)
        async def tool_exec(self, inp: acts.ToolExecInput) -> ToolResult:
            await self._enter(acts.TOOL_EXEC.name, inp)
            raise ApplicationError("the checkpoint push failed", type="CheckpointError")

    stubs = Failing(lambda turn: [call("edit_file", "c1")])
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)

    assert acts.ENSURE_SANDBOX.name not in stubs.names()
    # The activity's own policy retried it, three attempts in all.
    assert len(stubs.payloads(acts.TOOL_EXEC.name)) == acts.TOOL_EXEC.retry.maximum_attempts


async def test_a_run_that_starts_with_a_sandbox_ensures_it_before_the_first_turn(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # A run that carries a sandbox id comes from an earlier run, and the container may be gone.
    stubs = Stubs(lambda turn: [call("read_file", "c1")] if turn == 0 else [finish()])
    async with running(env, stubs) as queue:
        handle = await start_with(env, queue, resumed(spec))
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert stubs.names() == [
        "ensure_sandbox",
        "llm_turn",
        "tool_exec",
        "append_results",
        "llm_turn",
        "append_results",
    ]  # the workspace is not prepared again
    assert stubs.payloads(acts.ENSURE_SANDBOX.name) == [
        acts.EnsureSandboxInput(
            task_id=spec.task_id, head_sha=sha(7), sandbox_id="sbx-1", spec=spec
        )
    ]
    (tool,) = stubs.payloads(acts.TOOL_EXEC.name)
    assert (tool.pre_sha, tool.sandbox_id) == (sha(7), "sbx-2")
    assert outcome.kind is OutcomeKind.PR_OPENED
    assert final.last_checkpoint == sha(7)


async def test_a_run_that_starts_with_a_sandbox_is_preparing_while_it_is_ensured(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.hold(acts.ENSURE_SANDBOX.name)
    stubs.hold(acts.LLM_TURN.name)
    async with running(env, stubs) as queue:
        handle = await start_with(env, queue, resumed(spec))
        assert (await wait_for_phase(handle, Phase.PREPARING)).last_checkpoint == sha(7)
        stubs.release(acts.ENSURE_SANDBOX.name)
        await wait_for_phase(handle, Phase.CODING)
        stubs.release(acts.LLM_TURN.name)
        await result_of(handle)


async def test_ensure_sandbox_gets_the_timeout_and_retries_of_section_3_7(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs()
    async with running(env, stubs) as queue:
        handle = await start_with(env, queue, resumed(spec))
        await result_of(handle)
        ensure = scheduled_options(await handle.fetch_history())[acts.ENSURE_SANDBOX.name]

    assert timeouts(ensure) == (10 * 60, 60)
    assert ensure.retry_policy.maximum_attempts == 5


async def test_a_cancel_during_ensure_sandbox_waits_for_it_and_cleans_up(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.hold(acts.ENSURE_SANDBOX.name)  # never opened
    async with running(env, stubs) as queue:
        handle = await start_with(env, queue, resumed(spec), tuning=SHORT_HEARTBEAT)
        await wait_until(lambda: acts.ENSURE_SANDBOX.name in stubs.names(), "ensure to start")

        await handle.signal("cancel", args=["alice", "not needed"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.CANCELLED
    assert stubs.names() == ["ensure_sandbox", "ensure_sandbox:cancelled", "cleanup"]
    (cleanup,) = stubs.payloads(acts.CLEANUP.name)
    assert cleanup.sandbox_id == "sbx-1"  # the id the run carried: ensure never answered


async def test_a_failed_ensure_sandbox_fails_the_workflow_with_its_error(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    class NoRepo(Stubs):
        @activity.defn(name=acts.ENSURE_SANDBOX.name)
        async def ensure_sandbox(self, inp: acts.EnsureSandboxInput) -> str:
            await self._enter(acts.ENSURE_SANDBOX.name, inp)
            raise ApplicationError(
                "no checkpoint repo", type="RepoNotFoundError", non_retryable=True
            )

    stubs = NoRepo()
    async with running(env, stubs) as queue:
        handle = await start_with(env, queue, resumed(spec))
        with pytest.raises(WorkflowFailureError) as caught:
            await result_of(handle)

    activity_error = caught.value.cause
    assert isinstance(activity_error, ActivityError)
    assert isinstance(activity_error.cause, ApplicationError)
    assert activity_error.cause.type == "RepoNotFoundError"
    assert acts.LLM_TURN.name not in stubs.names()


async def test_a_run_with_a_sandbox_and_no_checkpoint_cannot_be_recreated(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    state = resumed(spec).model_copy(update={"head_sha": None})
    stubs = Stubs()
    async with running(env, stubs) as queue:
        handle = await start_with(env, queue, state)
        with pytest.raises(WorkflowFailureError) as caught:
            await result_of(handle)

    assert isinstance(caught.value.cause, ApplicationError)
    assert "no checkpoint" in str(caught.value.cause)
    assert stubs.names() == []


# ---------- Continue-As-New (LH-207) ----------
EVERY_3 = acts.Tuning(continue_as_new_every_turns=3)
HINT = "alice: use pytest -x"
TURN = [acts.LLM_TURN.name, acts.TOOL_EXEC.name, acts.APPEND_RESULTS.name]
# What a run does at its end, and what the next one does before its first turn.
HAND_OVER = [acts.BUNDLE_CHECKPOINT.name, acts.RECORD_EVENT.name, acts.ENSURE_SANDBOX.name]


def edits_then_finishes(last_turn: int) -> Script:
    """Every turn edits a file and so leaves a checkpoint, until `last_turn` calls finish."""

    def script(turn: int) -> list[acts.ToolCallSummary]:
        if turn == last_turn:
            return [finish()]
        return [call("edit_file", f"edit-{turn}", path="calc.py")]

    return script


def current_run(env: WorkflowEnvironment, spec: TaskSpec) -> WorkflowHandle[Any, Outcome]:
    """A handle without a run id, so a query reaches the run that is open now."""
    return env.client.get_workflow_handle(spec.task_id)


async def test_continue_as_new_preserves_state(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    # Ten turns, a hand-over every three: runs of turns 0-2, 3-5, 6-8 and 9.
    stubs = Stubs(edits_then_finishes(9), cost_usd=0.25)
    stubs.hold_turn(1)
    stubs.hold_turn(3)  # the first turn of the second run
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=EVERY_3)
        await wait_until(lambda: stubs.started(1), "turn 1")
        await handle.signal("hint", args=["alice", "use pytest -x"])
        stubs.release_turn(1)

        # The second run has started and has not asked the model anything yet, so a query
        # shows exactly what the first run handed over.
        await wait_until(lambda: stubs.started(3), "the second run's first turn")
        carried = await current_run(env, spec).query(TaskWorkflow.status)
        stubs.release_turn(3)

        outcome = await result_of(handle)
        final = await current_run(env, spec).query(TaskWorkflow.status)

    assert (carried.turn_index, carried.last_checkpoint) == (3, sha(3))
    assert carried.cost_usd == pytest.approx(0.75)
    assert carried.phase is Phase.CODING

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="fixed add()")
    assert (final.phase, final.turn_index, final.last_checkpoint) == (Phase.DONE, 10, sha(9))
    assert final.cost_usd == pytest.approx(2.5)

    # Four runs under the one workflow id, and each took its share of the turns.
    assert stubs.workflow_ids == {spec.task_id}
    assert len(stubs.run_ids) == 4
    by_run = [[t for t, run in stubs.turn_runs.items() if run == r] for r in stubs.run_ids]
    assert by_run == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]

    # At the end of a run: bundle and event. The next run puts its sandbox back at the head.
    assert stubs.names() == [
        "prepare_workspace",
        *TURN * 3,
        *HAND_OVER,
        *TURN * 3,
        *HAND_OVER,
        *TURN * 3,
        *HAND_OVER,
        acts.LLM_TURN.name,
        acts.APPEND_RESULTS.name,
    ]
    assert stubs.payloads(acts.BUNDLE_CHECKPOINT.name) == [
        acts.BundleCheckpointInput(task_id=spec.task_id, turn_index=n) for n in (3, 6, 9)
    ]
    events = stubs.payloads(acts.RECORD_EVENT.name)
    assert [(e.task_id, e.type) for e in events] == [(spec.task_id, "continue_as_new")] * 3
    assert [e.payload for e in events] == [
        {
            "reason": "turns",
            "turn_index": n,
            "head_sha": sha(n),
            "bundle": f"tasks/lh-acme-calc-42/ckpt/{n}.bundle",
        }
        for n in (3, 6, 9)
    ]
    # Each run asks about the sandbox the one before it ended with, and the stub answers with a
    # new id every time, so the id travels across the hand-overs as well.
    ensured = stubs.payloads(acts.ENSURE_SANDBOX.name)
    assert [(e.head_sha, e.sandbox_id) for e in ensured] == [
        (sha(3), "sbx-1"),
        (sha(6), "sbx-2"),
        (sha(9), "sbx-3"),
    ]
    tools = stubs.payloads(acts.TOOL_EXEC.name)
    assert [(t.turn_index, t.sandbox_id) for t in tools] == [
        (n, f"sbx-{n // 3 + 1}") for n in range(9)
    ]

    # The turns after the hint all carry it, and the turn that was running when it came does not.
    inputs = stubs.payloads(acts.LLM_TURN.name)
    assert {i.turn_index: i.hints for i in inputs} == {
        0: [],
        1: [],
        **{n: [HINT] for n in range(2, 10)},
    }
    # Each turn reads the transcript the one before it wrote, across the hand-over too.
    transcripts = {i.turn_index: i.transcript_ref for i in inputs}
    assert transcripts[3] == ref("turns/0002/results.json")
    assert transcripts[9] == ref("turns/0008/results.json")


async def test_signal_not_lost_across_can(env: WorkflowEnvironment) -> None:
    # The first run is held inside its hand-over when the signals come, which is the window in
    # which a signal could fall between two runs. The budget shows in where the run ends.
    spec = make_spec(task_turns=6)
    stubs = Stubs(never_finishes)
    stubs.hold(acts.BUNDLE_CHECKPOINT.name)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=EVERY_3)
        await wait_until(lambda: acts.BUNDLE_CHECKPOINT.name in stubs.names(), "the hand-over")
        await handle.signal("hint", args=["alice", "use pytest -x"])
        await handle.signal("add_budget", args=["alice", 2.5, 2])
        stubs.release(acts.BUNDLE_CHECKPOINT.name)

        outcome = await result_of(handle)
        final = await current_run(env, spec).query(TaskWorkflow.status)

    # Six turns and the two the top-up added.
    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="turn limit")
    assert (final.phase, final.turn_index) == (Phase.FAILED, 8)
    inputs = stubs.payloads(acts.LLM_TURN.name)
    assert [i.hints for i in inputs] == [[]] * 3 + [[HINT]] * 5
    # Hand-overs after turns 3 and 6: the second is allowed only because of the top-up.
    assert len(stubs.payloads(acts.BUNDLE_CHECKPOINT.name)) == 2
    assert len(stubs.run_ids) == 3


async def test_a_burst_of_hints_survives_many_hand_overs(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # A hand-over after every turn, and hints arriving the whole time. The state is built in
    # @workflow.init for this: a hint that lands in the first task of a run must not be dropped
    # (ADR-0004, appendix section 4).
    last, sent = 9, 30
    stubs = Stubs(edits_then_finishes(last))
    stubs.hold_turn(last)  # keeps the workflow open until the last hint is in
    async with running(env, stubs) as queue:
        handle = await start_task(
            env.client,
            spec,
            task_queue=queue,
            tuning=acts.Tuning(continue_as_new_every_turns=1),
        )
        for n in range(sent):
            await handle.signal("hint", args=["alice", f"hint {n}"])
            await asyncio.sleep(0.01)
        await wait_until(lambda: stubs.started(last), "the last turn")
        stubs.release_turn(last)
        await result_of(handle)

    assert len(stubs.run_ids) == last + 1
    assert stubs.payloads(acts.LLM_TURN.name)[-1].hints == [f"alice: hint {n}" for n in range(sent)]


async def test_a_run_that_has_used_its_turns_ends_instead_of_handing_over(
    env: WorkflowEnvironment,
) -> None:
    # Turn 3 is both the hand-over turn and the last turn the budget allows. A new run would
    # only find that out, after a bundle upload.
    stubs = Stubs(never_finishes)
    async with running(env, stubs) as queue:
        handle = await start_task(
            env.client, make_spec(task_turns=3), task_queue=queue, tuning=EVERY_3
        )
        outcome = await result_of(handle)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="turn limit")
    assert acts.BUNDLE_CHECKPOINT.name not in stubs.names()
    assert len(stubs.run_ids) == 1


async def test_a_run_that_finishes_on_the_hand_over_turn_does_not_hand_over(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(edits_then_finishes(2))  # the third turn finishes, and turn_index is 3 after it
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=EVERY_3)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert acts.BUNDLE_CHECKPOINT.name not in stubs.names()
    assert len(stubs.run_ids) == 1


async def test_a_cancel_that_arrives_during_the_hand_over_still_cancels(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(never_finishes)
    stubs.hold(acts.BUNDLE_CHECKPOINT.name)  # never opened
    tuning = acts.Tuning(continue_as_new_every_turns=3, heartbeat_timeout_s=2)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=tuning)
        await wait_until(lambda: acts.BUNDLE_CHECKPOINT.name in stubs.names(), "the hand-over")

        await handle.signal("cancel", args=["alice", "enough"])
        outcome = await result_of(handle)

    assert outcome == Outcome(kind=OutcomeKind.CANCELLED, summary="cancelled by alice: enough")
    # The bundle is stopped before the clean-up starts, and no second run is made.
    assert stubs.names()[-3:] == ["bundle_checkpoint", "bundle_checkpoint:cancelled", "cleanup"]
    assert acts.RECORD_EVENT.name not in stubs.names()
    assert len(stubs.run_ids) == 1


async def test_a_cancel_while_the_hand_over_event_is_written_still_cancels(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # The event is a local activity, which has its own way to be stopped.
    stubs = Stubs(never_finishes)
    stubs.hold(acts.RECORD_EVENT.name)  # never opened
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=EVERY_3)
        await wait_until(lambda: acts.RECORD_EVENT.name in stubs.names(), "the hand-over event")

        await handle.signal("cancel", args=["alice", "enough"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.CANCELLED
    assert stubs.names()[-3:] == ["record_event", "record_event:cancelled", "cleanup"]
    assert len(stubs.run_ids) == 1


async def test_a_cancel_after_the_hand_over_cancels_the_new_run(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # The signal goes to the workflow id, so it reaches the run that is open when it arrives.
    stubs = Stubs(never_finishes)
    stubs.hold(acts.ENSURE_SANDBOX.name)  # the new run's first step
    tuning = acts.Tuning(continue_as_new_every_turns=3, heartbeat_timeout_s=2)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=tuning)
        await wait_until(lambda: acts.ENSURE_SANDBOX.name in stubs.names(), "the second run")
        await handle.signal("cancel", args=["alice", "enough"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.CANCELLED
    assert len(stubs.run_ids) == 2
    (cleanup,) = stubs.payloads(acts.CLEANUP.name)
    assert cleanup.sandbox_id == "sbx-1"  # the id the first run handed over


async def test_a_bundle_or_event_that_cannot_be_written_does_not_stop_the_hand_over(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    # Both are a record for people and a safety net for a lost host. Failing the task for them
    # would trade a small loss for a large one, and the history still has to be reset.
    stubs = Stubs(edits_then_finishes(4))
    stubs.fails = {
        acts.BUNDLE_CHECKPOINT.name: ApplicationError(
            "the bundle is too large", type="CheckpointError", non_retryable=True
        ),
        acts.RECORD_EVENT.name: ApplicationError("database is down", type="OperationalError"),
    }
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=EVERY_3)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert len(stubs.run_ids) == 2
    assert len(stubs.payloads(acts.BUNDLE_CHECKPOINT.name)) == 1  # not retried: it cannot work
    assert len(stubs.payloads(acts.RECORD_EVENT.name)) == acts.RECORD_EVENT.retry.maximum_attempts


async def test_an_event_for_a_failed_bundle_says_there_is_no_bundle(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(edits_then_finishes(4))
    stubs.fails = {
        acts.BUNDLE_CHECKPOINT.name: ApplicationError(
            "no space left", type="CheckpointError", non_retryable=True
        )
    }
    async with running(env, stubs) as queue:
        await result_of(await start_task(env.client, spec, task_queue=queue, tuning=EVERY_3))

    (event,) = stubs.payloads(acts.RECORD_EVENT.name)
    assert event.payload["bundle"] is None


async def test_the_hand_over_activities_get_the_policies_of_section_3_7(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(edits_then_finishes(4))
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=EVERY_3)
        await result_of(handle)
        first_run = env.client.get_workflow_handle(
            spec.task_id, run_id=handle.first_execution_run_id
        )
        history = await first_run.fetch_history()

    sched = scheduled_options(history)
    assert timeouts(sched[acts.BUNDLE_CHECKPOINT.name]) == (5 * 60, 60)
    assert sched[acts.BUNDLE_CHECKPOINT.name].retry_policy.maximum_attempts == 3
    # A local activity is not scheduled as an activity task. It shows up as a marker.
    assert acts.RECORD_EVENT.name not in sched
    markers = [
        event
        for event in history.events
        if event.HasField("marker_recorded_event_attributes")
        and event.marker_recorded_event_attributes.marker_name == "core_local_activity"
    ]
    assert len(markers) == 1
    # The first run ended by continuing as new.
    assert history.events[-1].HasField("workflow_execution_continued_as_new_event_attributes")


async def test_recorded_histories_replay(env: WorkflowEnvironment) -> None:
    """The workflow makes the same commands when its own history is replayed (F13).

    One run takes signals and uses a tool before it finishes. One is cancelled in the middle of
    a turn. One finds its sandbox gone, recreates it and goes on. One hands over to a new run
    after its first turn, so both of its runs are replayed. One is the task of an issue, which is
    picked up and delivered as a pull request (LH-306).
    """
    stubs = Stubs(lambda turn: [call("edit_file", "c1")] if turn == 0 else [finish()])
    async with running(env, stubs) as queue:
        stubs.hold(acts.LLM_TURN.name)
        done = await start_task(env.client, make_spec("lh-acme-calc-1"), task_queue=queue)
        await wait_until(lambda: acts.LLM_TURN.name in stubs.names(), "the first turn")
        await done.signal("hint", args=["alice", "use pytest -x"])
        await done.signal("add_budget", args=["alice", 1.0, 5])
        await done.signal("approve_plan", args=["alice"])
        stubs.release(acts.LLM_TURN.name)
        await result_of(done)

        stubs.hold(acts.LLM_TURN.name)
        turns_so_far = len(stubs.payloads(acts.LLM_TURN.name))
        cancelled = await start_task(
            env.client, make_spec("lh-acme-calc-2"), task_queue=queue, tuning=SHORT_HEARTBEAT
        )
        await wait_until(
            lambda: len(stubs.payloads(acts.LLM_TURN.name)) > turns_so_far, "the second run's turn"
        )
        await cancelled.signal("cancel", args=["alice", "stop"])
        await result_of(cancelled)

        stubs.release(acts.LLM_TURN.name)  # the cancelled run left the gate closed
        stubs.gone["c1"] = 1
        recovered = await start_task(env.client, make_spec("lh-acme-calc-3"), task_queue=queue)
        await result_of(recovered)
        resumed_run = await start_with(env, queue, resumed(make_spec("lh-acme-calc-4")))
        await result_of(resumed_run)
        assert acts.ENSURE_SANDBOX.name in stubs.names()

        chained = await start_task(
            env.client,
            make_spec("lh-acme-calc-5"),
            task_queue=queue,
            tuning=acts.Tuning(continue_as_new_every_turns=1),
        )
        await result_of(chained)
        delivered = await start_task(env.client, github_spec("lh-acme-calc-6"), task_queue=queue)
        await result_of(delivered)
        first_run = env.client.get_workflow_handle(
            "lh-acme-calc-5", run_id=chained.first_execution_run_id
        )

        histories = [
            await done.fetch_history(),
            await cancelled.fetch_history(),
            await recovered.fetch_history(),
            await resumed_run.fetch_history(),
            await first_run.fetch_history(),
            await env.client.get_workflow_handle("lh-acme-calc-5").fetch_history(),
            await delivered.fetch_history(),
        ]

    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=pydantic_data_converter,
    )
    for history in histories:
        await replayer.replay_workflow(history)


# ---------- context: the compact activity and the retry after an overflow (LH-404) ----------
def reads_then_finishes(last_turn: int) -> Script:
    def script(turn: int) -> list[acts.ToolCallSummary]:
        if turn == last_turn:
            return [finish()]
        return [call("read_file", f"read-{turn}", path="calc.py")]

    return script


def compactions(stubs: Stubs) -> list[acts.CompactInput]:
    return stubs.payloads(acts.COMPACT.name)


def events_of(stubs: Stubs, event_type: str) -> list[dict[str, Any]]:
    return [e.payload for e in stubs.payloads(acts.RECORD_EVENT.name) if e.type == event_type]


async def test_a_compact_decision_compacts_the_view_before_the_next_turn(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(2))
    stubs.decisions[0] = "compact"
    outcome, _ = await play(env, stubs, spec)

    assert outcome.kind is OutcomeKind.PR_OPENED
    # The turn ends with its results in the view, and then the view is compacted.
    assert stubs.names() == [
        "prepare_workspace",
        *["llm_turn", "tool_exec", "append_results"],
        "compact",
        "record_event",
        *["llm_turn", "tool_exec", "append_results"],
        *["llm_turn", "append_results"],
    ]
    (request,) = compactions(stubs)
    assert request.task_id == spec.task_id
    assert request.turn_index == 0
    assert request.forced is False
    assert request.transcript_ref == ref("turns/0000/results.json")
    assert request.notes is None
    assert request.spec == spec


async def test_the_next_turn_starts_from_the_compacted_view_with_the_notes(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(2))
    stubs.decisions[0] = "compact"
    await play(env, stubs, spec)

    first, second, third = stubs.payloads(acts.LLM_TURN.name)
    assert first.notes is None
    assert second.transcript_ref == ref("turns/0000/compacted.json")
    assert second.notes == NOTES
    # The notes stay with the task: the turn after has them too.
    assert third.notes == NOTES


async def test_a_force_decision_compacts_with_the_forced_flag(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(1))
    stubs.decisions[0] = "force"
    await play(env, stubs, spec)

    (request,) = compactions(stubs)
    assert request.forced is True


async def test_a_later_round_hands_the_earlier_notes_to_the_compactor(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(3))
    stubs.decisions[0] = "compact"
    stubs.decisions[1] = "compact"
    await play(env, stubs, spec)

    first, second = compactions(stubs)
    assert first.notes is None
    assert second.notes == NOTES
    assert second.transcript_ref == ref("turns/0001/results.json")


async def test_the_compaction_is_an_event_with_the_tokens_before_and_after(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(1))
    stubs.decisions[0] = "compact"
    await play(env, stubs, spec)

    assert events_of(stubs, "compaction") == [
        {
            "turn_index": 0,
            "forced": False,
            "status": "ok",
            "tokens_before": 5_000,
            "tokens_after": 2_000,
            "folded_turns": 4,
        }
    ]


async def test_what_the_compactor_cost_is_added_to_the_cost_of_the_task(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(1), cost_usd=0.5)
    stubs.decisions[0] = "compact"
    _, final = await play(env, stubs, spec)

    assert final.cost_usd == pytest.approx(0.5 + 0.5 + 0.25)  # two model turns and one compaction


async def test_an_ok_decision_compacts_nothing(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Stubs(reads_then_finishes(3))
    await play(env, stubs, spec)

    assert acts.COMPACT.name not in stubs.names()
    assert events_of(stubs, "compaction") == []


async def test_a_turn_that_finishes_the_task_does_not_compact(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(0))
    stubs.decisions[0] = "force"
    await play(env, stubs, spec)

    assert acts.COMPACT.name not in stubs.names()


async def test_a_turn_without_tool_calls_is_compacted_after_its_nudge(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(lambda turn: [] if turn == 0 else [finish()])
    stubs.decisions[0] = "compact"
    await play(env, stubs, spec)

    assert stubs.names()[:5] == [
        "prepare_workspace",
        "llm_turn",
        "append_results",
        "compact",
        "record_event",
    ]
    (request,) = compactions(stubs)
    assert request.transcript_ref == ref("turns/0000/results.json")


async def test_a_compaction_that_fails_for_good_fails_the_workflow(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(2))
    stubs.decisions[0] = "compact"
    stubs.fails[acts.COMPACT.name] = ApplicationError(
        "no price for this model", type="PricingError", non_retryable=True
    )
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)


async def test_an_event_that_cannot_be_written_does_not_stop_a_compaction(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(1))
    stubs.decisions[0] = "compact"
    stubs.fails[acts.RECORD_EVENT.name] = ApplicationError("db down", non_retryable=True)
    outcome, _ = await play(env, stubs, spec)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert stubs.payloads(acts.LLM_TURN.name)[1].transcript_ref == ref("turns/0000/compacted.json")


async def test_compact_gets_the_timeout_and_retries_of_section_3_7(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(1))
    stubs.decisions[0] = "compact"
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue)
        await result_of(handle)
        sched = scheduled_options(await handle.fetch_history())

    attributes = sched[acts.COMPACT.name]
    assert timeouts(attributes) == (10 * 60, 30)
    assert attributes.retry_policy.maximum_attempts == 5
    assert {"invalid_request", "auth", "context_too_long"} <= set(
        attributes.retry_policy.non_retryable_error_types
    )


# ---------- F6: a prompt over the window ----------
async def test_a_prompt_over_the_window_is_compacted_and_asked_again_once(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(2))
    stubs.overflow[1] = 1
    outcome, final = await play(env, stubs, spec)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert stubs.names() == [
        "prepare_workspace",
        *["llm_turn", "tool_exec", "append_results"],
        "llm_turn",  # turn 1 is too long
        "compact",
        "record_event",
        *["llm_turn", "tool_exec", "append_results"],  # and goes through the second time
        *["llm_turn", "append_results"],
    ]
    (request,) = compactions(stubs)
    assert request.forced is True
    assert request.turn_index == 1
    assert request.transcript_ref == ref("turns/0000/results.json")
    retry = stubs.payloads(acts.LLM_TURN.name)[2]
    assert (retry.turn_index, retry.transcript_ref, retry.notes) == (
        1,
        ref("turns/0001/compacted.json"),
        NOTES,
    )
    assert final.turn_index == 3
    assert events_of(stubs, "compaction")[0]["forced"] is True


async def test_a_second_overflow_ends_the_run_as_it_did_before(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(2))
    stubs.overflow[1] = 2
    outcome, final = await play(env, stubs, spec)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="context limit")
    assert len(compactions(stubs)) == 1  # one forced compaction, and no second
    assert stubs.names().count(acts.LLM_TURN.name) == 3
    assert (final.phase, final.turn_index) == (Phase.FAILED, 2)


async def test_the_one_retry_belongs_to_the_turn(env: WorkflowEnvironment, spec: TaskSpec) -> None:
    stubs = Stubs(reads_then_finishes(3))
    stubs.overflow[1] = 1
    stubs.overflow[2] = 1
    outcome, _ = await play(env, stubs, spec)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert [c.turn_index for c in compactions(stubs)] == [1, 2]


async def test_an_overflow_with_nothing_to_cut_ends_the_run_without_asking_again(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    def nothing(inp: acts.CompactInput) -> acts.CompactOutput:
        return acts.CompactOutput(
            transcript_ref=inp.transcript_ref,
            notes=inp.notes,
            status="unchanged",
            tokens_before=900,
            tokens_after=900,
            folded_turns=0,
        )

    stubs = Stubs(reads_then_finishes(2))
    stubs.overflow[1] = 1
    stubs.compaction = nothing
    outcome, _ = await play(env, stubs, spec)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="context limit")
    assert stubs.names().count(acts.LLM_TURN.name) == 2  # turn 1 was not asked a second time


async def test_a_compaction_that_changed_nothing_is_not_an_event(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    def nothing(inp: acts.CompactInput) -> acts.CompactOutput:
        return acts.CompactOutput(
            transcript_ref=inp.transcript_ref,
            notes=inp.notes,
            status="unchanged",
            tokens_before=900,
            tokens_after=900,
            folded_turns=0,
        )

    stubs = Stubs(reads_then_finishes(2))
    stubs.decisions[0] = "compact"
    stubs.compaction = nothing
    outcome, _ = await play(env, stubs, spec)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert events_of(stubs, "compaction") == []
    assert stubs.payloads(acts.LLM_TURN.name)[1].transcript_ref == ref("turns/0000/results.json")


async def test_an_overflow_that_gets_a_forced_compaction_keeps_the_compaction_cost(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(1))
    stubs.overflow[1] = 1
    _, final = await play(env, stubs, spec)

    assert final.cost_usd == pytest.approx(0.25)


async def test_notes_and_the_compacted_view_cross_a_hand_over(
    env: WorkflowEnvironment, spec: TaskSpec
) -> None:
    stubs = Stubs(reads_then_finishes(3))
    stubs.decisions[0] = "compact"
    async with running(env, stubs) as queue:
        handle = await start_task(
            env.client, spec, task_queue=queue, tuning=acts.Tuning(continue_as_new_every_turns=2)
        )
        await result_of(handle)

    # Turn 2 is the first turn of the second run. It still has the notes.
    turn_two = stubs.payloads(acts.LLM_TURN.name)[2]
    assert (turn_two.turn_index, turn_two.notes) == (2, NOTES)
    assert turn_two.transcript_ref == ref("turns/0001/results.json")


async def test_histories_with_a_compaction_replay(env: WorkflowEnvironment) -> None:
    stubs = Stubs(reads_then_finishes(3))
    stubs.decisions[0] = "compact"
    stubs.overflow[2] = 1
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, make_spec("lh-acme-calc-7"), task_queue=queue)
        await result_of(handle)
        history = await handle.fetch_history()

    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=pydantic_data_converter,
    )
    await replayer.replay_workflow(history)


# ---------- LH-306: a task of an issue is delivered as a pull request ----------
PICKUP = [acts.POST_COMMENT.name, acts.ADD_LABEL.name]
DELIVERY = [
    acts.BUILD_PR_BRANCH.name,
    acts.DELIVER.name,
    acts.REMOVE_LABEL.name,
    acts.ADD_LABEL.name,
]
FINISHED = Outcome(kind=OutcomeKind.PR_OPENED, pr_url=PR_URL, summary="fixed add()")


async def test_a_task_of_an_issue_is_picked_up_first_and_ends_with_a_pull_request(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    outcome, final = await play(env, stubs, gh_spec)

    assert outcome == FINISHED
    assert (final.phase, final.outcome) == (Phase.DONE, FINISHED)
    # The pickup is the first activity, before the workspace exists. The loop is untouched.
    assert stubs.every == [*PICKUP, "prepare_workspace", "llm_turn", "append_results", *DELIVERY]
    assert stubs.names() == ["prepare_workspace", "llm_turn", "append_results"]

    (comment,) = stubs.payloads(acts.POST_COMMENT.name)
    assert comment == lifecycle.pickup_comment(gh_spec)
    assert (comment.repo, comment.number) == ("acme/calc", 42)
    working, swap_off, swap_on = (
        *stubs.payloads(acts.ADD_LABEL.name)[:1],
        *stubs.payloads(acts.REMOVE_LABEL.name),
        *stubs.payloads(acts.ADD_LABEL.name)[1:],
    )
    assert working == lifecycle.on_pickup(gh_spec)[0].label
    assert swap_off == lifecycle.on_pr_opened(gh_spec)[0].label
    assert swap_on == lifecycle.on_pr_opened(gh_spec)[1].label


async def test_the_pull_request_is_built_from_the_last_checkpoint_and_the_finish_summary(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs(lambda turn: [call("edit_file", "e1")] if turn == 0 else [finish("all done")])
    outcome, _ = await play(env, stubs, gh_spec)

    (build,) = stubs.payloads(acts.BUILD_PR_BRANCH.name)
    assert build == acts.BuildPrBranchInput(
        task_id=gh_spec.task_id,
        repo="acme/calc",
        summary="all done",
        base_sha=BASE,
        head_sha=sha(1),
    )
    (deliver,) = stubs.payloads(acts.DELIVER.name)
    assert deliver == acts.DeliverInput(
        task_id=gh_spec.task_id,
        repo="acme/calc",
        issue_number=42,
        summary="all done",
        branch="longhaul-pr/42",
        base_branch="main",
        base_sha=BASE,
        head_sha=sha(900),
        title="fix(calc): fix add",
    )
    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, pr_url=PR_URL, summary="all done")


async def test_the_phase_is_delivering_until_the_pull_request_is_open(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.hold(acts.DELIVER.name)
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, gh_spec, task_queue=queue)

        delivering = await wait_for_phase(handle, Phase.DELIVERING)
        assert delivering.outcome is None, "the task is not over until the pull request is open"
        assert delivering.last_checkpoint == BASE
        assert stubs.github_names().count(acts.ADD_LABEL.name) == 1  # working, not pr-open yet
        stubs.release(acts.DELIVER.name)

        outcome = await result_of(handle)
        done = await handle.query(TaskWorkflow.status)

    assert outcome == FINISHED
    assert (done.phase, done.outcome) == (Phase.DONE, FINISHED)


@pytest.mark.parametrize(
    "task",
    [
        pytest.param(make_spec(repo="local:/work/calc", issue=42), id="local folder with issue"),
        pytest.param(make_spec(repo="acme/calc", issue=None), id="repo without issue"),
        pytest.param(make_spec(repo="local:/work/calc", issue=None), id="local folder"),
    ],
)
async def test_other_tasks_keep_todays_behaviour(env: WorkflowEnvironment, task: TaskSpec) -> None:
    stubs = Stubs()
    outcome, final = await play(env, stubs, task)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="fixed add()")
    assert (final.phase, final.outcome) == (Phase.DONE, outcome)
    assert stubs.every == ["prepare_workspace", "llm_turn", "append_results"]


async def test_the_pickup_is_not_repeated_after_a_hand_over(env: WorkflowEnvironment) -> None:
    spec = github_spec()
    stubs = Stubs(edits_then_finishes(4))
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=EVERY_3)
        outcome = await result_of(handle)

    assert outcome == FINISHED
    assert len(stubs.run_ids) == 2
    assert stubs.github_names() == [*PICKUP, *DELIVERY]


async def test_the_base_of_the_pull_request_is_kept_across_a_hand_over(
    env: WorkflowEnvironment,
) -> None:
    # The workspace is prepared in the first run and the pull request is built in the second.
    # The base is the one the workspace was prepared at, and the workflow state carries it.
    stubs = Stubs(edits_then_finishes(4))
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, github_spec(), task_queue=queue, tuning=EVERY_3)
        await result_of(handle)

    assert len(stubs.run_ids) == 2
    (build,) = stubs.payloads(acts.BUILD_PR_BRANCH.name)
    assert build.base_sha == BASE
    assert build.head_sha == sha(4)


async def test_a_task_that_cannot_be_picked_up_does_no_work(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.fails = {
        acts.POST_COMMENT.name: ApplicationError(
            "Resource not accessible by personal access token",
            type=acts.GITHUB_FATAL,
            non_retryable=True,
        )
    }
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, gh_spec, task_queue=queue)
        with pytest.raises(WorkflowFailureError) as caught:
            await result_of(handle)

    # No model money is spent on a task that could not say it had begun.
    assert isinstance(caught.value.cause, ActivityError)
    assert stubs.names() == []
    assert stubs.github_names() == [acts.POST_COMMENT.name]


async def test_the_github_writes_are_retried_by_the_policy_of_section_3_7(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.fails = {
        acts.ADD_LABEL.name: ApplicationError("GitHub is down", type=acts.GITHUB_RETRYABLE)
    }
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, gh_spec, task_queue=queue)
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)

    assert stubs.github_names().count(acts.ADD_LABEL.name) == acts.ADD_LABEL.retry.maximum_attempts
    assert stubs.names() == []


async def test_a_task_that_changed_nothing_ends_with_an_error_and_no_pull_request(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.fails = {
        acts.BUILD_PR_BRANCH.name: ApplicationError(
            "the work does not change any file", type=acts.NOTHING_TO_DELIVER, non_retryable=True
        )
    }
    outcome, final = await play(env, stubs, gh_spec)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="nothing to deliver: no file changed")
    assert (final.phase, final.outcome) == (Phase.FAILED, outcome)
    assert stubs.github_names() == [*PICKUP, acts.BUILD_PR_BRANCH.name]


async def test_a_delivery_that_fails_for_good_leaves_the_working_label(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.fails = {
        acts.DELIVER.name: ApplicationError(
            "push rejected", type="CheckpointError", non_retryable=True
        )
    }
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, gh_spec, task_queue=queue)
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)

    # The labels follow the pull request: without one, the issue still says "working".
    assert stubs.github_names() == [*PICKUP, acts.BUILD_PR_BRANCH.name, acts.DELIVER.name]


async def test_a_cancel_while_delivering_cleans_up_and_swaps_no_label(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    stubs.hold(acts.DELIVER.name)  # never opened
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, gh_spec, task_queue=queue, tuning=SHORT_HEARTBEAT)
        await wait_for_phase(handle, Phase.DELIVERING)
        await wait_until(lambda: acts.DELIVER.name in stubs.github_names(), "deliver to start")

        await handle.signal("cancel", args=["alice", "wrong issue"])
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(kind=OutcomeKind.CANCELLED, summary="cancelled by alice: wrong issue")
    assert final.phase is Phase.CANCELLED
    assert stubs.github_names() == [
        *PICKUP,
        acts.BUILD_PR_BRANCH.name,
        acts.DELIVER.name,
        "deliver:cancelled",
    ]
    assert stubs.names()[-1] == acts.CLEANUP.name


async def test_the_github_activities_get_the_policies_of_section_3_7(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    async with running(env, stubs) as queue:
        handle = await start_task(env.client, gh_spec, task_queue=queue)
        await result_of(handle)
        sched = scheduled_options(await handle.fetch_history())

    for act in (acts.POST_COMMENT, acts.ADD_LABEL, acts.REMOVE_LABEL):
        assert timeouts(sched[act.name]) == (2 * 60, None)
        assert sched[act.name].retry_policy.maximum_attempts == 10
        assert sched[act.name].retry_policy.maximum_interval.ToTimedelta().total_seconds() == 300
    assert timeouts(sched[acts.BUILD_PR_BRANCH.name]) == (5 * 60, None)
    assert sched[acts.BUILD_PR_BRANCH.name].retry_policy.maximum_attempts == 3
    assert timeouts(sched[acts.DELIVER.name]) == (10 * 60, 60)
    assert sched[acts.DELIVER.name].retry_policy.maximum_attempts == 5
    assert acts.CREATE_PR.name not in sched, "deliver calls create_pr itself, inside the activity"


@workflow.defn(sandboxed=False)
class PauseProbe:
    """Pauses and resumes a task twice with the helpers that Phases 5 and 7 will call.

    Nothing calls `_needs_human` and `_human_done` yet, so this workflow builds a TaskWorkflow
    from the state it is given and calls them, the way the loop will. It is not sandboxed: the
    sandbox would load this test module a second time.
    """

    @workflow.run
    async def run(self, state: CarryState) -> None:
        task = TaskWorkflow(state)
        await task._needs_human(1)
        await task._human_done(1)
        await task._needs_human(2)


async def pause_and_resume(env: WorkflowEnvironment, stubs: Stubs, task: TaskSpec) -> None:
    async with running(env, stubs, workflows=[PauseProbe]) as queue:
        await env.client.execute_workflow(
            PauseProbe.run,
            initial_state(task),
            id=f"pause-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
        )


async def test_a_pause_adds_the_needs_human_label_and_a_resume_removes_it(
    env: WorkflowEnvironment, gh_spec: TaskSpec
) -> None:
    stubs = Stubs()
    await pause_and_resume(env, stubs, gh_spec)

    # Three label activities, in the order of the calls. Each pause has a key of its own, and the
    # resume of a pause has another, so a repeated step is a no-op on GitHub and a new pause is
    # a new effect.
    assert stubs.every == [acts.ADD_LABEL.name, acts.REMOVE_LABEL.name, acts.ADD_LABEL.name]
    first_pause, second_pause = stubs.payloads(acts.ADD_LABEL.name)
    (resume,) = stubs.payloads(acts.REMOVE_LABEL.name)
    assert first_pause == lifecycle.on_pause(gh_spec, 1)[0].label
    assert resume == lifecycle.on_resume(gh_spec, 1)[0].label
    assert second_pause == lifecycle.on_pause(gh_spec, 2)[0].label
    assert {first_pause.label, resume.label, second_pause.label} == {lifecycle.NEEDS_HUMAN}
    assert (first_pause.repo, first_pause.number) == ("acme/calc", 42)
    assert (
        len({first_pause.idempotency_key, resume.idempotency_key, second_pause.idempotency_key})
        == 3
    )
    assert stubs.names() == [], "nothing but labels is written"


@pytest.mark.parametrize(
    "task",
    [
        pytest.param(make_spec(repo="local:/work/calc", issue=42), id="local folder with issue"),
        pytest.param(make_spec(repo="acme/calc", issue=None), id="repo without issue"),
    ],
)
async def test_a_task_that_is_not_an_issue_has_no_label_to_change_on_a_pause(
    env: WorkflowEnvironment, task: TaskSpec
) -> None:
    stubs = Stubs()
    await pause_and_resume(env, stubs, task)

    assert stubs.every == []
