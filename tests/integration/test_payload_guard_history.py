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
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

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
from longhaul.contracts import Budgets, RepoSettings, TaskSpec
from longhaul.durable import acts
from longhaul.durable.client import start_task
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.payload_guard import DEFAULT_LIMIT, PayloadTooLargeError
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
        issue_number=42,
        title="add() subtracts",
        body=body,
        settings=RepoSettings(repo="acme/calc", budgets=Budgets(task_turns=3)),
    )


class Stubs:
    """The activities a task needs before its first model call. The model call itself stops."""

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        return acts.PrepareWorkspaceOutput(sandbox_id="sbx-1", base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        raise ApplicationError("the stand-in model stops here", non_retryable=True)


def task_worker(env: WorkflowEnvironment, queue: str) -> Worker:
    stubs = Stubs()
    return Worker(
        env.client,
        task_queue=queue,
        workflows=[TaskWorkflow],
        activities=[stubs.prepare_workspace, stubs.llm_turn],
        workflow_runner=workflow_runner(),
    )


def scheduled(history: WorkflowHistory) -> list[str]:
    """The type of every activity that the history says was scheduled, in order."""
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]


def event_types(history: WorkflowHistory) -> list[int]:
    return [e.event_type for e in history.events]


def assert_no_big_payload(history: WorkflowHistory, *, largest_seen_over: int = 0) -> None:
    """Every payload is under the cap. `largest_seen_over` proves that the scan saw something."""
    sizes = payload_sizes(history)
    assert sizes, "the scan found no payloads, so it proves nothing"
    assert max(sizes) < DEFAULT_LIMIT
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

    Two hints of 200 KB are each under the limit as signals. llm_turn carries every hint, so
    its input is 400 KB. The workflow cannot schedule it, and the run ends with the guard's error
    instead of a workflow task that fails and retries without end.
    """
    handle = await start_task(env.client, make_spec(), task_queue=queue)
    for who in ("ann", "bob"):
        await handle.signal(TaskWorkflow.hint, args=[who, "h" * 200_000])

    async with task_worker(env, queue):
        with pytest.raises(WorkflowFailureError) as caught:
            await asyncio.wait_for(handle.result(), WAIT_S)

    cause = caught.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == OVERSIZED
    assert "blob store" in cause.message

    history = await handle.fetch_history()
    assert scheduled(history) == [acts.PREPARE_WORKSPACE.name]  # llm_turn was never scheduled
    assert EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED not in event_types(history)
    assert event_types(history)[-1] == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED
    # The two signals are in the history, which shows the scan reaches payloads of that size.
    assert_no_big_payload(history, largest_seen_over=200_000)


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
    assert scheduled(history) == [acts.PREPARE_WORKSPACE.name, acts.LLM_TURN.name]
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


@workflow.defn(name="ProbeContinueAsNew", failure_exception_types=[PayloadTooLargeError])
class ProbeContinueAsNew:
    @workflow.run
    async def run(self, text: str) -> int:
        if text.startswith("hand-over-"):
            workflow.continue_as_new("x" * int(text.removeprefix("hand-over-")))
        return len(text)


@pytest.fixture
async def probe_queue(env: WorkflowEnvironment, queue: str) -> AsyncIterator[str]:
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[ProbeActivityResult, ProbeContinueAsNew],
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


async def test_a_continue_as_new_over_the_limit_fails_the_workflow(
    env: WorkflowEnvironment, probe_queue: str
) -> None:
    ok = await run_probe(env, probe_queue, ProbeContinueAsNew.run, "hand-over-200000")
    assert await asyncio.wait_for(ok.result(), WAIT_S) == 200_000

    handle = await run_probe(env, probe_queue, ProbeContinueAsNew.run, "hand-over-300000")
    with pytest.raises(WorkflowFailureError) as caught:
        await asyncio.wait_for(handle.result(), WAIT_S)

    assert isinstance(caught.value.cause, ApplicationError)
    assert caught.value.cause.type == OVERSIZED
    history = await handle.fetch_history()
    assert EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW not in event_types(history)
    assert_no_big_payload(history)
