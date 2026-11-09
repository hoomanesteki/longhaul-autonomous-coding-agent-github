"""LH-292: invariant I2 in the data converter, against a Temporal test server.

The activity-side guard (`assert_small`) runs after the workflow has scheduled an activity, so an
oversized input is in the history by then. The converter guard runs where the payload is made:
on the client for a start, a signal or a query, in the workflow for an activity input or a
Continue-As-New, and in the worker for an activity result. These tests send an oversized value
down each of those paths and read the history afterwards. It must hold no payload over 256 KB,
and no event for the work that the value was meant for.

The worker runs under the sandbox settings of a production worker, and the client uses the data
converter of `durable.client.connect`.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowFailureError, WorkflowHandle, WorkflowHistory
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from tests.fakes.history import payload_sizes

from longhaul import cli
from longhaul.agent.hints import CUT_NOTE, MAX_HINT_BYTES
from longhaul.contracts import (
    BlobRef,
    Budgets,
    CarryState,
    Phase,
    RepoSettings,
    Role,
    TaskSpec,
    ToolResult,
    Usage,
)
from longhaul.durable import acts
from longhaul.durable.client import start_task
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.payload_guard import DEFAULT_LIMIT, PayloadTooLargeError, json_size
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github.intake import MAX_BODY_BYTES

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
BASE = "b" * 40
WAIT_S = 30.0
OVERSIZED = PayloadTooLargeError.__name__


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=guarded_data_converter
    ) as environment:
        yield environment


@pytest.fixture
def queue() -> str:
    return f"lh-test-{uuid.uuid4().hex[:8]}"


def make_spec(body: str = "`calc.add` subtracts its arguments instead of adding them.") -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}",
        repo="acme/calc",
        # No issue number: the task of an issue on an owner/name repo is delivered as a pull
        # request (LH-306), and this test runs the loop on stub activities with no GitHub.
        issue_number=None,
        title="add() subtracts",
        body=body,
        settings=RepoSettings(repo="acme/calc", budgets=Budgets(task_turns=3)),
    )


def ref(name: str) -> BlobRef:
    return BlobRef(key=f"tasks/lh-acme-calc-42/{name}", sha256="0" * 64, size=1)


class Stubs:
    """The activities a task needs. The model call either stops the run or asks for one tool.

    With `model_stops` the run ends at the first model call, which is as far as the tests of an
    input need. Without it the model asks for `read_file` and never finishes, so the run goes on
    to a hand-over.
    """

    def __init__(self, *, model_stops: bool = True) -> None:
        self.model_stops = model_stops

    @property
    def activities(self) -> list[Callable[..., Any]]:
        return [
            self.prepare_workspace,
            self.llm_turn,
            self.tool_exec,
            self.append_results,
            self.bundle_checkpoint,
            self.record_event,
            self.cleanup,
        ]

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        return acts.PrepareWorkspaceOutput(sandbox_id="sbx-1", base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        if self.model_stops:
            raise ApplicationError("the stand-in model stops here", non_retryable=True)
        return acts.LlmTurnOutput(
            transcript_ref=ref("assistant.json"),
            tool_call_summaries=[
                acts.ToolCallSummary(id="read-0", name="read_file", input={"path": "calc.py"})
            ],
            stop_reason="tool_use",
            usage=Usage(),
            cost_usd=0.0,
        )

    @activity.defn(name=acts.TOOL_EXEC.name)
    async def tool_exec(self, inp: acts.ToolExecInput) -> ToolResult:
        return ToolResult(call_id=inp.call.id, ok=True, output="ran", pre_sha=inp.pre_sha)

    @activity.defn(name=acts.APPEND_RESULTS.name)
    async def append_results(self, inp: acts.AppendResultsInput) -> BlobRef:
        return ref("results.json")

    @activity.defn(name=acts.BUNDLE_CHECKPOINT.name)
    async def bundle_checkpoint(self, inp: acts.BundleCheckpointInput) -> BlobRef:
        return ref("ckpt.bundle")

    @activity.defn(name=acts.RECORD_EVENT.name)
    async def record_event(self, inp: acts.RecordEventInput) -> None:
        return None

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        return None


def task_worker(env: WorkflowEnvironment, queue: str, stubs: Stubs | None = None) -> Worker:
    return Worker(
        env.client,
        task_queue=queue,
        workflows=[TaskWorkflow],
        activities=(stubs or Stubs()).activities,
        workflow_runner=workflow_runner(),
    )


def scheduled(history: WorkflowHistory) -> list[str]:
    """The type of every activity that the history says was scheduled, in order.

    The writes of the task row (LH-891) are left out: they are bookkeeping that the console reads,
    and these tests are about the activities that carry the work of the task.
    """
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
        and e.activity_task_scheduled_event_attributes.activity_type.name
        != acts.RECORD_TASK_STATE.name
    ]


def event_types(history: WorkflowHistory) -> list[int]:
    return [e.event_type for e in history.events]


def assert_no_big_payload(history: WorkflowHistory, *, largest_seen_over: int = 0) -> None:
    """Every payload is under the cap. `largest_seen_over` proves that the scan saw something."""
    sizes = payload_sizes(history)
    assert sizes, "the scan found no payloads, so it proves nothing"
    assert max(sizes) <= DEFAULT_LIMIT
    assert max(sizes) > largest_seen_over


# ---------- the client ----------
async def test_a_task_with_a_300_kb_body_is_refused_before_it_is_started(
    env: WorkflowEnvironment, queue: str
) -> None:
    spec = make_spec(body="x" * 300_000)

    with pytest.raises(PayloadTooLargeError):
        await start_task(env.client, spec, task_queue=queue)

    with pytest.raises(RPCError) as caught:  # the server never heard of it
        await env.client.get_workflow_handle(spec.task_id).describe()
    assert caught.value.status is RPCStatusCode.NOT_FOUND


async def test_a_signal_or_a_query_over_the_limit_is_refused_before_it_is_sent(
    env: WorkflowEnvironment, queue: str
) -> None:
    handle = await start_task(env.client, make_spec(), task_queue=queue)  # no worker polls

    with pytest.raises(PayloadTooLargeError):
        await handle.signal(TaskWorkflow.hint, args=["ann", "x" * 300_000])
    with pytest.raises(PayloadTooLargeError):
        await handle.query("status", "x" * 300_000)

    history = await handle.fetch_history()
    assert EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED not in event_types(history)
    assert_no_big_payload(history)
    await handle.terminate()


# ---------- the workflow ----------
async def test_an_activity_input_over_the_limit_fails_the_workflow_and_is_never_written(
    env: WorkflowEnvironment, queue: str
) -> None:
    """The case of the gate review: each piece is small, the input that the workflow builds is not.

    The workflow cuts each hint to MAX_HINT_BYTES (LH-506), so no single hint can do this any
    more. A hundred and fifty hints of that size are each far under the limit as signals.
    llm_turn carries every hint, so its input is over the limit. The workflow cannot schedule it,
    and the run ends with the guard's error instead of a workflow task that fails and retries
    without end.
    """
    handle = await start_task(env.client, make_spec(), task_queue=queue)
    texts = ["h" * MAX_HINT_BYTES] * 150
    assert sum(len(t) for t in texts) > DEFAULT_LIMIT
    for n, text in enumerate(texts):
        await handle.signal(TaskWorkflow.hint, args=[f"user{n}", text])

    async with task_worker(env, queue):
        with pytest.raises(WorkflowFailureError) as caught:
            await asyncio.wait_for(handle.result(), WAIT_S)

    cause = caught.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == OVERSIZED
    assert "blob store" in cause.message

    history = await handle.fetch_history()
    # llm_turn was never scheduled. The task still winds up (LH-507), so its sandbox goes.
    assert scheduled(history) == [acts.PREPARE_WORKSPACE.name, acts.CLEANUP.name]
    assert EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED not in event_types(history)
    assert event_types(history)[-1] == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED
    # The signals are in the history, which shows the scan reaches the payloads of the run.
    assert_no_big_payload(history, largest_seen_over=MAX_HINT_BYTES)


async def test_a_hint_over_the_cap_is_cut_before_it_is_kept(
    env: WorkflowEnvironment, queue: str
) -> None:
    """A 200 KB hint is under the limit as a signal, and the state keeps only the cut text.

    Before LH-506 two of them made llm_turn's input 400 KB and failed the task. Now the task
    runs: the stand-in model stops it at the first turn, and the input it was given is small.
    """
    seen: list[acts.LlmTurnInput] = []

    class Spy(Stubs):
        @activity.defn(name=acts.LLM_TURN.name)
        async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
            seen.append(inp)
            raise ApplicationError("the stand-in model stops here", non_retryable=True)

    handle = await start_task(env.client, make_spec(), task_queue=queue)
    for who in ("ann", "bob"):
        await handle.signal(TaskWorkflow.hint, args=[who, "h" * 200_000])

    async with task_worker(env, queue, Spy()):
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), WAIT_S)

    assert len(seen) == 1
    assert len(seen[0].hints) == 2
    assert all(len(h.encode()) < MAX_HINT_BYTES + 200 for h in seen[0].hints)
    assert all(h.endswith(CUT_NOTE) for h in seen[0].hints)
    assert json_size(seen[0]) < 20_000


async def test_a_300_kb_issue_file_runs_and_the_history_stays_small(
    env: WorkflowEnvironment, queue: str, tmp_path: Path
) -> None:
    # What `longhaul run --issue-file big.md` builds: the file is cut to the limit of a GitHub
    # issue, so the carry state and the input of every activity stay small.
    issue = tmp_path / "big.md"
    issue.write_text("# Big issue\n\n" + "x" * 300_000, encoding="utf-8")
    title, body = cli.read_issue(issue)
    spec = cli.durable_spec(CALC, f"lh-local-calc-{uuid.uuid4().hex[:8]}", title, body)

    handle = await start_task(env.client, spec, task_queue=queue)
    async with task_worker(env, queue):
        with pytest.raises(WorkflowFailureError) as caught:  # the stand-in model stops the run
            await asyncio.wait_for(handle.result(), WAIT_S)

    assert isinstance(caught.value.cause, ActivityError)
    assert isinstance(caught.value.cause.cause, ApplicationError)
    assert caught.value.cause.cause.type is None  # the stand-in's error, not the guard's
    history = await handle.fetch_history()
    # The wind-up of the failed task (LH-507) destroys its sandbox last.
    assert scheduled(history) == [
        acts.PREPARE_WORKSPACE.name,
        acts.LLM_TURN.name,
        acts.CLEANUP.name,
    ]
    assert_no_big_payload(history, largest_seen_over=MAX_BODY_BYTES)
    assert max(payload_sizes(history)) < 20_000


# ---------- an activity result and Continue-As-New ----------
# Workflows of the tests, outside the production sandbox: they have nothing to do with I1, and
# the converter that runs is the same object either way.
@activity.defn(name="probe_big_result")
async def probe_big_result(size: int) -> str:
    return "x" * size


@workflow.defn(name="ProbeActivityResult")
class ProbeActivityResult:
    @workflow.run
    async def run(self, size: int) -> int:
        text = await workflow.execute_activity(
            probe_big_result,
            size,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return len(text)


@pytest.fixture
async def probe_queue(env: WorkflowEnvironment, queue: str) -> AsyncIterator[str]:
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[ProbeActivityResult],
        activities=[probe_big_result],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        yield queue


async def run_probe(
    env: WorkflowEnvironment, probe_queue: str, workflow_run: object, arg: object
) -> WorkflowHandle[object, int]:
    return await env.client.start_workflow(
        workflow_run,  # type: ignore[arg-type]
        arg,
        id=f"probe-{uuid.uuid4().hex[:8]}",
        task_queue=probe_queue,
    )


async def test_an_activity_result_over_the_limit_fails_the_activity(
    env: WorkflowEnvironment, probe_queue: str
) -> None:
    ok = await run_probe(env, probe_queue, ProbeActivityResult.run, 200_000)
    assert await asyncio.wait_for(ok.result(), WAIT_S) == 200_000

    handle = await run_probe(env, probe_queue, ProbeActivityResult.run, 300_000)
    with pytest.raises(WorkflowFailureError) as caught:
        await asyncio.wait_for(handle.result(), WAIT_S)

    assert isinstance(caught.value.cause, ActivityError)
    assert isinstance(caught.value.cause.cause, ApplicationError)
    assert caught.value.cause.cause.type == OVERSIZED
    assert_no_big_payload(await handle.fetch_history())


def hints_that_fill(spec: TaskSpec) -> list[str]:
    """Hint texts that make the llm_turn input of turn 0 exactly as big as the limit allows.

    Each hint is cut to MAX_HINT_BYTES, so the texts are as many full-size hints as it takes and
    one that fills the rest. The state at the hand-over after that turn holds the same hints,
    the ref of the transcript, the sandbox and the checkpoint, so it is over the limit. The test
    checks that, so it cannot pass by accident if either model changes.
    """
    plain = acts.LlmTurnInput(
        task_id=spec.task_id, turn_index=0, role=Role.CODER, spec=spec, transcript_ref=None
    )
    # A hint is stored as "ann: <text>".
    stored: list[str] = []
    while True:
        room = DEFAULT_LIMIT - json_size(plain.model_copy(update={"hints": [*stored, "ann: "]}))
        if room <= MAX_HINT_BYTES:
            break
        stored.append("ann: " + "h" * MAX_HINT_BYTES)
    stored.append("ann: " + "h" * room)
    assert json_size(plain.model_copy(update={"hints": stored})) == DEFAULT_LIMIT
    handed_over = CarryState(
        spec=spec,
        phase=Phase.CODING,
        turn_index=1,
        transcript_ref=ref("results.json"),
        head_sha=BASE,
        sandbox_id="sbx-1",
        started_at=datetime.now(UTC),
        hints=stored,
    )
    assert json_size(handed_over) > DEFAULT_LIMIT
    return [h.removeprefix("ann: ") for h in stored]


async def test_a_hand_over_with_a_state_over_the_limit_fails_the_workflow(
    env: WorkflowEnvironment, queue: str
) -> None:
    """The SDK drops an error from the arguments of a Continue-As-New and leaves the run open.

    TaskWorkflow converts its state first, so the run ends with the guard's error. The state of
    a hand-over is a few hundred bytes larger than the llm_turn input that carries the same
    hints, so hints can fit in the one and push the other over. These fit the model call to the
    byte.
    """
    spec = make_spec()
    texts = hints_that_fill(spec)
    handle = await start_task(
        env.client, spec, task_queue=queue, tuning=acts.Tuning(continue_as_new_every_turns=1)
    )
    for text in texts:
        await handle.signal(TaskWorkflow.hint, args=["ann", text])

    async with task_worker(env, queue, Stubs(model_stops=False)):
        with pytest.raises(WorkflowFailureError) as caught:
            await asyncio.wait_for(handle.result(), WAIT_S)

    cause = caught.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == OVERSIZED
    history = await handle.fetch_history()
    assert acts.LLM_TURN.name in scheduled(history)  # the turn ran: the input was just small enough
    assert EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW not in event_types(history)
    assert_no_big_payload(history, largest_seen_over=250_000)
