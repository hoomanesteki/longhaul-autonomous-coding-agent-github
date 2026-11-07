"""LH-706: the log lines of an activity that a worker runs.

A one-activity workflow runs on a worker whose client carries the tracing interceptor and the
logging interceptor, the way durable.client.connect builds it. The activity logs through
structlog, through the standard library and through Temporal's own activity logger. A time
skipping test server stands in for the Temporal stack, and an in-memory exporter stands in for
the collector, so the test can compare a log line with the span that was open when it was
written.
"""

import logging
import uuid
from datetime import timedelta
from typing import Any

import pytest
import structlog
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.client import WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker
from tests.fakes.log_capture import captured_logs

from longhaul.telemetry import logging as longhaul_logging
from longhaul.telemetry import otel
from longhaul.telemetry.logging import REDACTED

pytestmark = [pytest.mark.integration, pytest.mark.phase7]

ACTIVITY_NAME = "say_something"
GITHUB_TOKEN = "ghp_" + "b" * 36


class SayInput(BaseModel):
    task_id: str
    turn_index: int
    fail: bool = False


@activity.defn(name=ACTIVITY_NAME)
async def say_something(inp: SayInput) -> None:
    structlog.get_logger("test.structlog").info("from structlog", token=GITHUB_TOKEN)
    logging.getLogger("test.stdlib").info("from the standard library")
    activity.logger.info("from the activity logger")
    if inp.fail:
        raise ApplicationError("gave up", non_retryable=True)


@workflow.defn(name="SayWorkflow")
class SayWorkflow:
    @workflow.run
    async def run(self, inp: SayInput) -> None:
        await workflow.execute_activity(
            ACTIVITY_NAME,
            inp,
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


class Run:
    """What one workflow run left behind: its log lines and its RunActivity span."""

    def __init__(self, inp: SayInput, lines: list[dict[str, Any]], spans: list[ReadableSpan]):
        self.inp = inp
        self.lines = lines
        (self.span,) = [s for s in spans if s.name == f"RunActivity:{ACTIVITY_NAME}"]
        self.trace_id = format(self.span.context.trace_id, "032x")
        self.text = "\n".join(str(line) for line in lines)

    def where(self, start: str) -> dict[str, Any]:
        """The one line whose event begins with this text."""
        (found,) = [line for line in self.lines if line["event"].startswith(start)]
        return found


async def run_say(*, fail: bool = False) -> Run:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    inp = SayInput(task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}", turn_index=3, fail=fail)

    with captured_logs() as out, otel.configured(provider):
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=pydantic_data_converter,
            interceptors=[*otel.interceptors(), *longhaul_logging.interceptors()],
        ) as env:
            queue = f"lh-logging-{uuid.uuid4().hex[:8]}"
            async with Worker(
                env.client,
                task_queue=queue,
                workflows=[SayWorkflow],
                activities=[say_something],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ):
                handle = await env.client.start_workflow(
                    SayWorkflow.run, inp, id=inp.task_id, task_queue=queue
                )
                if fail:
                    with pytest.raises(WorkflowFailureError):
                        await handle.result()
                else:
                    await handle.result()
        run = Run(inp, out.lines(), list(exporter.get_finished_spans()))
    provider.shutdown()
    return run


async def test_context_bound() -> None:
    run = await run_say()

    # The activity logged through three loggers and all three lines are in the JSON output.
    mine = [
        run.where("from structlog"),
        run.where("from the standard library"),
        run.where("from the activity logger"),
    ]
    for line in mine:
        assert line["task_id"] == run.inp.task_id
        assert line["turn_index"] == 3
        assert line["activity"] == ACTIVITY_NAME
        assert line["trace_id"] == run.trace_id

    # The same lines carry no secret.
    assert run.where("from structlog")["token"] == REDACTED
    assert GITHUB_TOKEN not in run.text


async def test_the_failure_warning_of_the_sdk_names_the_task() -> None:
    run = await run_say(fail=True)

    # temporalio writes this line itself, after the activity function has raised.
    line = run.where("Completing activity as failed")
    assert line["level"] == "warning"
    assert line["task_id"] == run.inp.task_id
    assert line["activity"] == ACTIVITY_NAME
    assert line["turn_index"] == 3
    assert "gave up" in line["exception"]
    # The RunActivity span is closed by then, and an operator still needs the id to find it.
    assert line["trace_id"] == run.trace_id
