"""The client side of the durable layer: connect to Temporal, start a task, and ask about it.

Every client and worker uses the pydantic data converter with the size guard of invariant I2
(durable/payload_converter.py). Without the pydantic converter the SDK falls back to the
pydantic v1 methods, warns on every encode, and returns plain dicts where a model is expected
(ADR-0004, appendix section 2). Without the guard a payload over 256 KB would reach the history
(ADR-0005). A worker takes its converter from its client, so connecting here is enough.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from longhaul.context.modes import ContextMode
from longhaul.contracts import BlobRef, CarryState, Outcome, Phase, TaskSpec, TaskStatus
from longhaul.durable.acts import (
    CONTEXT_MODE_MEMO_KEY,
    PLAN_MEMO_KEY,
    SCRIPT_MEMO_KEY,
    TUNING_MEMO_KEY,
    Tuning,
)
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.telemetry import logging as logging_context
from longhaul.telemetry import otel

# The default of the task_queue setting (longhaul.settings.EnvSettings). A unit test keeps the
# two equal.
DEFAULT_TASK_QUEUE = "longhaul"


async def connect(address: str, *, namespace: str = "default") -> Client:
    """Connect to Temporal.

    The client carries two interceptors. When tracing is set up (telemetry.otel.setup_tracing),
    Temporal's tracing interceptor comes first, and the logging interceptor after it, so the
    RunActivity span is open when the logging one binds the trace id. A worker built on this
    client uses both, so workflow and activity spans share one trace and the log lines of an
    activity carry its task_id, turn_index and activity, without a second registration.
    """
    options: dict[str, Any] = {
        "interceptors": [*otel.interceptors(), *logging_context.interceptors()]
    }
    try:
        return await Client.connect(
            address, namespace=namespace, data_converter=guarded_data_converter, **options
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
    context_mode: ContextMode | None = None,
    plan: bool = False,
) -> WorkflowHandle[TaskWorkflow, Outcome]:
    """Start the workflow for a task. Its id is the task id.

    `script` names the uploaded script that the scripted provider plays for this task
    (longhaul.durable.scripts). It rides in the memo with the tuning, because the worker is a
    process of its own and started before the task. So does `context_mode`, the mode this run
    asks for in place of the config's (longhaul.context.modes); None leaves it to the config.

    `plan` has the planner write a plan before the coder starts (LH-501). A task started without
    it goes straight to coding. The poller and `longhaul run --plan` ask for it. It rides in the
    memo as well, so it holds for every run of the Continue-As-New chain.

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
    if context_mode is not None:
        memo[CONTEXT_MODE_MEMO_KEY] = context_mode
    if plan:
        memo[PLAN_MEMO_KEY] = True
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
