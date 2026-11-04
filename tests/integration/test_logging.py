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

import pytest
import structlog
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.contrib.pydantic import pydantic_data_converter
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


@activity.defn(name=ACTIVITY_NAME)
async def say_something(inp: SayInput) -> None:
    structlog.get_logger("test.structlog").info("from structlog", token=GITHUB_TOKEN)
    logging.getLogger("test.stdlib").info("from the standard library")
    activity.logger.info("from the activity logger")


@workflow.defn(name="SayWorkflow")
class SayWorkflow:
    @workflow.run
    async def run(self, inp: SayInput) -> None:
        await workflow.execute_activity(
            ACTIVITY_NAME, inp, start_to_close_timeout=timedelta(seconds=30)
        )


async def test_context_bound() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    inp = SayInput(task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}", turn_index=3)

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
                await env.client.execute_workflow(
                    SayWorkflow.run, inp, id=inp.task_id, task_queue=queue
                )
        lines = out.lines()
        spans = exporter.get_finished_spans()
    provider.shutdown()

    (run,) = [s for s in spans if s.name == f"RunActivity:{ACTIVITY_NAME}"]
    trace_id = format(run.context.trace_id, "032x")

    # The activity logged through three loggers and all three lines are in the JSON output.
    wanted = {"from structlog", "from the standard library"}
    mine = [line for line in lines if line["event"] in wanted]
    mine += [line for line in lines if line["event"].startswith("from the activity logger")]
    assert len(mine) == 3
    for line in mine:
        assert line["task_id"] == inp.task_id
        assert line["turn_index"] == 3
        assert line["activity"] == ACTIVITY_NAME
        assert line["trace_id"] == trace_id

    # The same lines carry no secret.
    assert next(line for line in mine if line["event"] == "from structlog")["token"] == REDACTED
    assert GITHUB_TOKEN not in out.text()
