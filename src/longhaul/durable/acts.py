"""What TaskWorkflow and its activities agree on: names, payload types and run policies.

The workflow calls every activity by name, with `result_type`, and imports none of the
activity modules. Those modules reach the Docker SDK, boto3, the model SDKs and SQLAlchemy,
which invariant I1 keeps out of workflow code. This module has no I/O, so both sides can
import it. The workflow reads the types and the policies. An activity registers under
`Act.name` and takes the input type as its parameter, so the two cannot drift apart.

The policies are the ones in plan section 3.7. Two activities are not in that table and got
their numbers here: `append_results`, which reads and writes one blob, and `cleanup`. Both are
sized like `load_memory`.

The fields of the payloads follow section 3.7 as well. LH-204 implements `llm_turn`,
`tool_exec` and `append_results` against them and may add fields. The workflow reads only the
ones it uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import NoneType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy

from longhaul.contracts import (
    BlobRef,
    Plan,
    ProgressNotes,
    ProviderErrorKind,
    Role,
    StopReason,
    TaskSpec,
    ToolResult,
    Usage,
)
from longhaul.durable.payload_guard import PayloadTooLargeError

# The `finish` control tool ends a task. The workflow reads its summary from the call and
# runs nothing for it. The name is copied from longhaul.tools.control.FINISH, so that the
# workflow does not load the tools package for one string. A unit test keeps the two equal.
FINISH_TOOL = "finish"
NO_SUMMARY = "finished without a summary"

# The longest a single tool command may run, in seconds. The workflow cannot read the config
# file, so the number is copied from `sandbox.command_timeout_s`. A unit test keeps the two
# equal.
TOOL_TIMEOUT_S = 600

# The workflow memo key under which a run carries its Tuning.
TUNING_MEMO_KEY = "tuning"


# ---------- payloads ----------
class PrepareWorkspaceOutput(BaseModel):
    sandbox_id: str
    base_sha: str
    head_sha: str


class ToolCallSummary(BaseModel):
    """A tool call as the workflow sees it, small enough for the history.

    The arguments are inline in `input`, or in the blob store under `args_ref` when they are
    over 16 KB (CLAUDE.md rule 5). Exactly one of the two is set.
    """

    id: str  # the provider's tool_use id
    name: str
    input: dict[str, Any] | None = None
    args_ref: BlobRef | None = None

    @model_validator(mode="after")
    def _arguments_are_inline_or_by_ref(self) -> Self:
        if (self.input is None) == (self.args_ref is None):
            raise ValueError("set exactly one of input and args_ref")
        return self


class LlmTurnInput(BaseModel):
    task_id: str
    turn_index: int
    role: Role
    transcript_ref: BlobRef | None  # None before the first turn
    notes: ProgressNotes | None = None
    plan: Plan | None = None
    memory_ref: BlobRef | None = None
    hints: list[str] = Field(default_factory=list)


class LlmTurnOutput(BaseModel):
    transcript_ref: BlobRef  # the transcript with this turn's reply appended
    tool_calls_ref: BlobRef | None = None  # every call with its full arguments
    tool_call_summaries: list[ToolCallSummary] = Field(default_factory=list)
    stop_reason: StopReason
    usage: Usage
    cost_usd: float = Field(default=0.0, ge=0)  # 0.0 until the pricing table arrives (LH-701)
    input_tokens_total: int = Field(default=0, ge=0)


class ToolExecInput(BaseModel):
    task_id: str
    turn_index: int
    call: ToolCallSummary
    pre_sha: str | None  # the checkpoint the call must start from
    sandbox_id: str


class AppendResultsInput(BaseModel):
    task_id: str
    turn_index: int
    transcript_ref: BlobRef
    results: list[ToolResult]


class CleanupInput(BaseModel):
    task_id: str
    # None when the workflow stopped before it learned the id. The activity then finds the
    # task's containers by their task label.
    sandbox_id: str | None
    reason: str


# ---------- activities ----------
@dataclass(frozen=True)
class Act[In, Out]:
    """One activity as the workflow calls it. `In` and `Out` carry the types to mypy."""

    name: str
    arg_type: type[In]
    result_type: type[Out]
    start_to_close: timedelta
    retry: RetryPolicy
    heartbeat: timedelta | None = None


# Retrying cannot make a payload smaller (ADR-0005), and the payload guard raises this type.
_OVERSIZED = PayloadTooLargeError.__name__

PREPARE_WORKSPACE = Act(
    "prepare_workspace",
    TaskSpec,
    PrepareWorkspaceOutput,
    start_to_close=timedelta(minutes=15),
    heartbeat=timedelta(seconds=60),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

LLM_TURN = Act(
    "llm_turn",
    LlmTurnInput,
    LlmTurnOutput,
    start_to_close=timedelta(minutes=10),
    heartbeat=timedelta(seconds=30),
    retry=RetryPolicy(
        initial_interval=timedelta(seconds=2),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=120),
        maximum_attempts=8,
        # The activity raises these as ApplicationError(type=kind.value, non_retryable=True).
        # The list is the second guard if one is raised without the flag.
        non_retryable_error_types=[
            ProviderErrorKind.INVALID_REQUEST.value,
            ProviderErrorKind.AUTH.value,
            ProviderErrorKind.CONTEXT_TOO_LONG.value,
            _OVERSIZED,
        ],
    ),
)

TOOL_EXEC = Act(
    "tool_exec",
    ToolExecInput,
    ToolResult,
    start_to_close=timedelta(seconds=TOOL_TIMEOUT_S + 60),
    heartbeat=timedelta(seconds=15),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

APPEND_RESULTS = Act(
    "append_results",
    AppendResultsInput,
    BlobRef,
    start_to_close=timedelta(minutes=2),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

CLEANUP = Act(
    "cleanup",
    CleanupInput,
    NoneType,
    start_to_close=timedelta(minutes=2),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

ALL: tuple[Act[Any, Any], ...] = (PREPARE_WORKSPACE, LLM_TURN, TOOL_EXEC, APPEND_RESULTS, CLEANUP)


# ---------- per-run tuning ----------
class Tuning(BaseModel):
    """Per-run changes to the section 3.7 timeouts, for tests and chaos runs.

    After a hard kill the server notices a dead activity only when its heartbeat timeout
    passes: up to 60 s for `prepare_workspace`, 30 s for `llm_turn`, 15 s for `tool_exec`
    (ADR-0004 section 3). A chaos run that kills the worker five times cannot wait that long,
    so it starts its tasks with a shorter heartbeat timeout.

    The value travels in the workflow memo, not in a global, so it works for a worker in
    another process, survives a replay and carries over Continue-As-New. A production run
    has no tuning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The server fires the timeout on its own clock, so the activity has to heartbeat well
    # inside it. The Python SDK sends at most one heartbeat per 0.8 timeouts.
    heartbeat_timeout_s: float | None = Field(default=None, gt=0)


def options(act: Act[Any, Any], tuning: Tuning | None = None) -> workflow.ActivityConfig:
    """The keyword arguments for workflow.start_activity: timeouts, retries and cancellation.

    When the workflow cancels an activity, it waits until the activity has stopped
    (WAIT_CANCELLATION_COMPLETED). The clean-up that follows removes the sandbox and the
    volume, and an activity that is still running, a `prepare_workspace` in the middle of a
    clone for one, could create them again after the sweep and leave them behind. The SDK's
    default (TRY_CANCEL) does not wait.

    The activity learns about the cancel at its next heartbeat, and the SDK sends at most one
    heartbeat per 0.8 timeouts. A cancel can therefore take up to 48 s while the workspace is
    prepared, 24 s during a model turn and 12 s during a tool call. If the worker is gone, the
    heartbeat timeout ends the wait. An activity without a heartbeat timeout cannot be
    interrupted, so only short ones have none.

    A tuning can only shorten a heartbeat timeout. An activity without one keeps none.
    """
    heartbeat = act.heartbeat
    if heartbeat is not None and tuning is not None and tuning.heartbeat_timeout_s is not None:
        heartbeat = min(heartbeat, timedelta(seconds=tuning.heartbeat_timeout_s))
    return {
        "start_to_close_timeout": act.start_to_close,
        "heartbeat_timeout": heartbeat,
        "retry_policy": act.retry,
        "cancellation_type": workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    }


def finish_summary(call: ToolCallSummary) -> str:
    """The text the model gave to `finish`, or a fixed line when it gave none."""
    text = (call.input or {}).get("summary")
    if isinstance(text, str) and text.strip():
        return text.strip()
    return NO_SUMMARY
