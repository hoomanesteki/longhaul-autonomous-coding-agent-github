"""The client side of the durable layer: connect to Temporal, start a task, and ask about it.

Every client and worker uses the pydantic data converter. Without it the SDK falls back to
the pydantic v1 methods, warns on every encode, and returns plain dicts where a model is
expected (ADR-0004, appendix section 2). A worker takes its converter from its client, so
connecting here is enough.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter

from longhaul.contracts import BlobRef, CarryState, Outcome, Phase, TaskSpec, TaskStatus
from longhaul.durable.acts import SCRIPT_MEMO_KEY, TUNING_MEMO_KEY, Tuning
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.telemetry import otel

# The default of the task_queue setting (longhaul.settings.EnvSettings). A unit test keeps the
# two equal.
DEFAULT_TASK_QUEUE = "longhaul"


async def connect(address: str, *, namespace: str = "default") -> Client:
    """Connect to Temporal.

    When tracing is set up (telemetry.otel.setup_tracing), the client carries Temporal's tracing
    interceptor. A worker built on this client uses it too, so workflow and activity spans share
    one trace without a second registration.
    """
    options: dict[str, Any] = {}
    if tracing := otel.interceptors():
        options["interceptors"] = tracing
    try:
        return await Client.connect(
            address, namespace=namespace, data_converter=pydantic_data_converter, **options
        )
    except RuntimeError as exc:  # the SDK raises this for a server it cannot reach
        raise ConnectionError(f"cannot reach Temporal at {address}: {exc}") from exc


async def start_task(
    client: Client,
    spec: TaskSpec,
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
    tuning: Tuning | None = None,
    script: BlobRef | None = None,
) -> WorkflowHandle[TaskWorkflow, Outcome]:
    """Start the workflow for a task. Its id is the task id.

    `script` names the uploaded script that the scripted provider plays for this task
    (longhaul.durable.scripts). It rides in the memo with the tuning, because the worker is a
    process of its own and started before the task.

    A second start with the same id raises WorkflowAlreadyStartedError, whether the first run
    is still open or has closed (ADR-0004 section 6). The SDK default would start a new run
    once the old one closed, and the poller would rerun a finished task for as long as the
    label stays on the issue. Namespace retention bounds the second case, so the `tasks`
    table stays the record of what ran.
    """
    state = CarryState(spec=spec, phase=Phase.QUEUED, started_at=datetime.now(UTC))
    memo: dict[str, Any] = {}
    if tuning is not None:
        memo[TUNING_MEMO_KEY] = tuning
    if script is not None:
        memo[SCRIPT_MEMO_KEY] = script
    # Each task is one trace. The poller starts tasks from an activity, where a current span
    # would otherwise make every task of a poll a child of that activity.
    with otel.detached():
        return await client.start_workflow(
            TaskWorkflow.run,
            state,
            id=spec.task_id,
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            memo=memo or None,
        )


def get_task(client: Client, task_id: str) -> WorkflowHandle[TaskWorkflow, Outcome]:
    """The handle of a task that was started earlier. The task id is the workflow id."""
    return client.get_workflow_handle_for(TaskWorkflow.run, task_id)


async def read_status(
    handle: WorkflowHandle[TaskWorkflow, Outcome], *, timeout_s: float
) -> TaskStatus:
    """Ask the workflow for status(). It fails after `timeout_s` when no worker answers.

    A query is answered by a worker, not by the server, so with every worker stopped it would
    wait for the server's own limit. A caller that polls sets a short deadline instead.
    """
    return await handle.query(TaskWorkflow.status, rpc_timeout=timedelta(seconds=timeout_s))


async def cancel_task(client: Client, task_id: str, *, by: str, reason: str) -> None:
    """Send the cancel signal. The workflow cleans up and ends with the CANCELLED outcome."""
    await get_task(client, task_id).signal(TaskWorkflow.cancel, args=[by, reason])
