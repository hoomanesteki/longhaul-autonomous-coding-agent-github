"""The worker's sandbox settings refuse an I/O library at run time (second guard of I1).

The source scan in tests/unit/test_invariants.py is the first guard. This test shows that the
second one does something: a workflow that imports docker inside run() is stopped by the
sandbox with a message that names the library. Without `invalid_modules` the same import
fails too, but by accident, and with a message about http.client (ADR-0004, appendix section 1).
"""

import asyncio
import importlib
import sys
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.durable.workflow_sandbox import workflow_runner

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

WORKFLOW_MODULE = """
from temporalio import workflow


@workflow.defn
class ImportsDocker:
    @workflow.run
    async def run(self) -> str:
        import docker  # noqa: F401

        return "imported"


@workflow.defn
class ImportsJson:
    @workflow.run
    async def run(self) -> str:
        import json  # noqa: F401

        return "fine"
"""


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """A module with two workflows, written to a temp directory and put on sys.path.

    The sandbox imports the workflow's module again by name, so it has to be importable.
    """
    name = f"probe_io_workflow_{uuid.uuid4().hex[:8]}"
    (tmp_path / f"{name}.py").write_text(WORKFLOW_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield importlib.import_module(name)
    finally:
        sys.modules.pop(name, None)


async def failed_task_message(handle: WorkflowHandle, seconds: float = 20.0) -> str:
    """The message of the first failed workflow task. The SDK retries these without end."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        async for event in handle.fetch_history_events():
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
                return " ".join(event.workflow_task_failed_event_attributes.failure.message.split())
        await asyncio.sleep(0.1)
    raise AssertionError("the workflow task never failed")


async def test_a_workflow_that_imports_an_io_library_is_stopped_by_the_sandbox(
    env: WorkflowEnvironment, probe: object
) -> None:
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[probe.ImportsDocker, probe.ImportsJson],  # type: ignore[attr-defined]
        workflow_runner=workflow_runner(),
    ):
        # A harmless import goes through, so the failure below is about docker.
        fine = await env.client.execute_workflow(
            probe.ImportsJson.run,  # type: ignore[attr-defined]
            id="imports-json",
            task_queue=queue,
        )
        assert fine == "fine"

        handle = await env.client.start_workflow(
            probe.ImportsDocker.run,  # type: ignore[attr-defined]
            id="imports-docker",
            task_queue=queue,
        )
        try:
            message = await failed_task_message(handle)
        finally:
            await handle.terminate()

    assert message.startswith("Cannot access docker from inside a workflow")
