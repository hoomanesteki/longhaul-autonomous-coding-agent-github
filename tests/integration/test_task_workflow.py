"""TaskWorkflow against a time skipping Temporal test server, with stub activities.

The stubs take the place of the activities. They are registered under the names in acts.py, so
the workflow cannot tell them from the real ones. These tests check what the workflow itself
does with what an activity returns: the phases, the signals, the order of the calls and how
each stop reason is handled. tests/integration/test_activities.py runs the real activities, and
tests/integration/test_parity.py compares the whole loop with the in-process one. Each test
gets its own server and worker, so nothing is shared between tests. The first run downloads the
test server (about 65 MB, ADR-0004); after that it starts in under a second.

The worker runs under the same sandbox settings as a production worker (invalid_modules for
the I/O libraries), so every test here also shows that the workflow loads under them.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from longhaul.agent.rules import BAD_FINISH, MAX_PAUSES, NUDGE
from longhaul.contracts import (
    BlobRef,
    Budgets,
    CarryState,
    Outcome,
    OutcomeKind,
    Phase,
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

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

BASE = "b" * 40
WAIT_S = 30.0
# A cancel reaches a running activity at its next heartbeat, and the SDK sends one at most
# every 0.8 heartbeat timeouts. The timeouts of section 3.7 would make each cancel test wait
# 24 s or more.
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=2)


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
    """

    MUTATING = frozenset({"edit_file", "write_file"})

    def __init__(self, script: Script | None = None, *, cost_usd: float = 0.0) -> None:
        self.script: Script = script or (lambda turn: [finish()])
        self.cost_usd = cost_usd
        self.log: list[tuple[str, Any]] = []
        self.gates = {act.name: asyncio.Event() for act in acts.ALL}
        for gate in self.gates.values():
            gate.set()
        self.commits = 0

    @property
    def activities(self) -> list[Callable[..., Any]]:
        return [
            self.prepare_workspace,
            self.llm_turn,
            self.tool_exec,
            self.append_results,
            self.cleanup,
        ]

    def hold(self, name: str) -> None:
        self.gates[name].clear()

    def release(self, name: str) -> None:
        self.gates[name].set()

    def names(self) -> list[str]:
        return [name for name, _ in self.log]

    def payloads(self, name: str) -> list[Any]:
        return [payload for logged, payload in self.log if logged == name]

    async def _enter(self, name: str, payload: Any) -> None:
        self.log.append((name, payload))
        try:
            while not self.gates[name].is_set():
                activity.heartbeat()
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            # The workflow cancelled this activity, or the worker is shutting down.
            self.log.append((f"{name}:cancelled", None))
            raise

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        await self._enter(acts.PREPARE_WORKSPACE.name, spec)
        return acts.PrepareWorkspaceOutput(sandbox_id="sbx-1", base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        await self._enter(acts.LLM_TURN.name, inp)
        return acts.LlmTurnOutput(
            transcript_ref=ref(f"turns/{inp.turn_index:04d}/assistant.json"),
            tool_call_summaries=self.script(inp.turn_index),
            stop_reason="tool_use",
            usage=Usage(),
            cost_usd=self.cost_usd,
        )

    @activity.defn(name=acts.TOOL_EXEC.name)
    async def tool_exec(self, inp: acts.ToolExecInput) -> ToolResult:
        await self._enter(acts.TOOL_EXEC.name, inp)
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


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def make_spec(task_id: str = "lh-acme-calc-42", *, task_turns: int | None = None) -> TaskSpec:
    budgets = Budgets() if task_turns is None else Budgets(task_turns=task_turns)
    return TaskSpec(
        task_id=task_id,
        repo="acme/calc",
        issue_number=42,
        title="add() subtracts",
        body="add(2, 3) returns -1",
        settings=RepoSettings(repo="acme/calc", budgets=budgets),
    )


@pytest.fixture
def spec() -> TaskSpec:
    return make_spec()


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
async def running(env: WorkflowEnvironment, stubs: Stubs) -> AsyncIterator[str]:
    """A worker for the workflow and the stubs. Yields the task queue it polls."""
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[TaskWorkflow],
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


async def test_recorded_histories_replay(env: WorkflowEnvironment) -> None:
    """The workflow makes the same commands when its own history is replayed (F13).

    One run takes signals and uses a tool before it finishes. The other is cancelled in the
    middle of a turn.
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

        histories = [await done.fetch_history(), await cancelled.fetch_history()]

    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=pydantic_data_converter,
    )
    for history in histories:
        await replayer.replay_workflow(history)
