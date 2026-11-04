"""`serve`, the loop of the worker process, against a Temporal test server (LH-208).

One stub activity stands in for the real ones: it holds until the test lets it go, and it
heartbeats while it waits, as a real activity does. The tests check what a stop does to an
activity that is running at that moment. The real process, with its signals and its real
activities, is tests/e2e/test_durable_run.py.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

from longhaul.contracts import RepoSettings, TaskSpec
from longhaul.durable import acts
from longhaul.durable.client import start_task
from longhaul.durable.worker import serve

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

WAIT_S = 30.0


class HoldingPrepare:
    """prepare_workspace that waits for `release`, and notes when it starts and when it is cut."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        self.started.set()
        try:
            while not self.release.is_set():
                activity.heartbeat()
                await asyncio.sleep(0.02)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return acts.PrepareWorkspaceOutput(sandbox_id="sbx-1", base_sha="b" * 40, head_sha="b" * 40)


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def spec() -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().int % 10_000}",
        repo="acme/calc",
        issue_number=42,
        title="add() subtracts",
        body="add(2, 3) returns -1",
        settings=RepoSettings(repo="acme/calc"),
    )


async def within(seconds: float, what: asyncio.Event) -> None:
    async with asyncio.timeout(seconds):
        await what.wait()


async def test_a_stop_waits_for_the_activity_that_is_running(env: WorkflowEnvironment) -> None:
    stub = HoldingPrepare()
    stop = asyncio.Event()
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    serving = asyncio.create_task(
        serve(env.client, queue, [stub.prepare_workspace], stop, grace=timedelta(seconds=30))
    )
    handle = await start_task(env.client, spec(), task_queue=queue)
    await within(WAIT_S, stub.started)

    stop.set()
    await asyncio.sleep(0.5)
    assert not serving.done(), "the worker left while an activity was running"
    assert not stub.cancelled.is_set()

    stub.release.set()
    async with asyncio.timeout(WAIT_S):
        await serving
    assert not stub.cancelled.is_set()
    await handle.terminate()


async def test_a_stop_gives_up_on_an_activity_after_the_grace_period(
    env: WorkflowEnvironment,
) -> None:
    stub = HoldingPrepare()  # never released
    stop = asyncio.Event()
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    serving = asyncio.create_task(
        serve(env.client, queue, [stub.prepare_workspace], stop, grace=timedelta(seconds=1))
    )
    handle = await start_task(env.client, spec(), task_queue=queue)
    await within(WAIT_S, stub.started)

    began = time.monotonic()
    stop.set()
    async with asyncio.timeout(WAIT_S):
        await serving

    assert time.monotonic() - began < WAIT_S
    assert stub.cancelled.is_set(), (
        "the activity should have been cancelled, to be retried elsewhere"
    )
    await handle.terminate()


async def test_serve_returns_at_once_when_nothing_is_running(env: WorkflowEnvironment) -> None:
    stop = asyncio.Event()
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    serving = asyncio.create_task(serve(env.client, queue, [], stop))
    await asyncio.sleep(0.2)
    assert not serving.done()

    stop.set()
    async with asyncio.timeout(WAIT_S):
        await serving
