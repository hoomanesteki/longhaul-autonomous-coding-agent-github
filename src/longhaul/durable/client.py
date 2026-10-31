"""The client side of the durable layer: connect to Temporal, and start a task.

Every client and worker uses the pydantic data converter. Without it the SDK falls back to
the pydantic v1 methods, warns on every encode, and returns plain dicts where a model is
expected (ADR-0004, appendix section 2). A worker takes its converter from its client, so
connecting here is enough.
"""

from __future__ import annotations

from datetime import UTC, datetime

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter

from longhaul.contracts import CarryState, Outcome, Phase, TaskSpec
from longhaul.durable.acts import TUNING_MEMO_KEY, Tuning
from longhaul.durable.workflows.task import TaskWorkflow

# The default of the task_queue setting (longhaul.settings.EnvSettings). A unit test keeps the
# two equal.
DEFAULT_TASK_QUEUE = "longhaul"


async def connect(address: str, *, namespace: str = "default") -> Client:
    return await Client.connect(
        address, namespace=namespace, data_converter=pydantic_data_converter
    )


async def start_task(
    client: Client,
    spec: TaskSpec,
    *,
    task_queue: str = DEFAULT_TASK_QUEUE,
    tuning: Tuning | None = None,
) -> WorkflowHandle[TaskWorkflow, Outcome]:
    """Start the workflow for a task. Its id is the task id.

    A second start with the same id raises WorkflowAlreadyStartedError, whether the first run
    is still open or has closed (ADR-0004 section 6). The SDK default would start a new run
    once the old one closed, and the poller would rerun a finished task for as long as the
    label stays on the issue. Namespace retention bounds the second case, so the `tasks`
    table stays the record of what ran.
    """
    state = CarryState(spec=spec, phase=Phase.QUEUED, started_at=datetime.now(UTC))
    return await client.start_workflow(
        TaskWorkflow.run,
        state,
        id=spec.task_id,
        task_queue=task_queue,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
        memo={TUNING_MEMO_KEY: tuning} if tuning is not None else None,
    )
