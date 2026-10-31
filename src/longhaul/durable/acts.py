"""What TaskWorkflow and its activities agree on: names, payload types and run policies.

The workflow calls every activity by name, with `result_type`, and imports none of the
activity modules. Those modules reach the Docker SDK, boto3, the model SDKs and SQLAlchemy,
which invariant I1 keeps out of workflow code. This module has no I/O, so both sides can
import it. The workflow reads the types and the policies. An activity registers under
`Act.name` and takes the input type as its parameter, so the two cannot drift apart.

The policies are the ones in plan section 3.7. Two activities are not in that table and got
their numbers here: `append_results`, which reads and writes one blob, and `cleanup`. Both are
sized like `load_memory`.

The fields of the payloads follow section 3.7 as well. LH-204 implemented `llm_turn`,
`tool_exec` and `append_results` against them and added fields. The workflow reads only the
ones it uses.

What crosses an activity boundary stays small (ADR-0005, invariants I2):

- A model turn returns refs to its transcript and its reply, and a summary of each tool call:
  the arguments inline up to INLINE_LIMIT_BYTES for one call and INLINE_ARGS_TOTAL_BYTES for
  the whole reply, and by `args_ref` beyond that.
- A tool call returns a `ToolResult` whose `output` is a preview of at most
  OUTPUT_PREVIEW_CHARS and whose `output_ref` holds the whole text. The text the model reads is
  the one in the blob.
- `append_results` takes one `ToolResultSummary` for each result, which names that blob. A turn
  of fifty calls with 15 KB of output each therefore adds a few KB to the history, and not
  750 KB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import NoneType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy

from longhaul.agent import rules
from longhaul.contracts import (
    BlobRef,
    Plan,
    ProgressNotes,
    ProviderErrorKind,
    Role,
    StopDetails,
    StopReason,
    TaskSpec,
    ToolResult,
    Usage,
)
from longhaul.durable.payload_guard import PayloadTooLargeError

# The `finish` control tool ends a task. The workflow runs nothing for it: rules.run_calls reads
# its summary. A unit test keeps the name equal to longhaul.tools.control.FINISH.
FINISH_TOOL = rules.FINISH
NO_SUMMARY = "finished without a summary"

# Anything over this crosses an activity boundary as a BlobRef (CONTRIBUTING.md rule 5).
INLINE_LIMIT_BYTES = 16_000
# The arguments of one reply that may travel inline together. Twelve calls of 10 KB each are
# all under the limit above and still add up to half the payload cap, so once a reply has this
# much inline the rest of its calls go by reference.
INLINE_ARGS_TOTAL_BYTES = 96_000
# How much of a tool's output stays in the `ToolResult` that the workflow history keeps. The
# rest is in the blob, and the model reads it from there.
OUTPUT_PREVIEW_CHARS = 2_000

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
    over 16 KB (CONTRIBUTING.md rule 5). Exactly one of the two is set.
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
    # The system prompt is built from it, in the same bytes on every call of a transcript (D7).
    spec: TaskSpec
    transcript_ref: BlobRef | None  # None before the first turn
    # Not used yet: the notes, the plan and the memory block reach the model in Phases 4 to 6, and
    # a hint reaches it with the pause and hint flow of LH-506.
    notes: ProgressNotes | None = None
    plan: Plan | None = None
    memory_ref: BlobRef | None = None
    hints: list[str] = Field(default_factory=list)
    # D8: the one retry of a turn that stopped on max_tokens runs at the higher cap. The
    # activity maps this to the number in the config, which the workflow cannot read.
    high_output_cap: bool = False
    # The text of a reply that stopped on pause_turn, for the call that continues it.
    paused_ref: BlobRef | None = None


class LlmTurnOutput(BaseModel):
    """What one model call came to.

    `transcript_ref` is the transcript to carry on from. A reply that is complete (end_turn or
    tool_use) is appended to it. Any other reply is not stored, so the ref is the input's, or the
    first message alone when the turn had no transcript yet. The workflow decides what to do
    next from `stop_reason`, with the rules in longhaul.agent.rules.
    """

    transcript_ref: BlobRef
    tool_calls_ref: BlobRef | None = None  # every call with its full arguments
    tool_call_summaries: list[ToolCallSummary] = Field(default_factory=list)
    stop_reason: StopReason
    usage: Usage
    cost_usd: float = Field(default=0.0, ge=0)  # 0.0 until the pricing table arrives (LH-701)
    input_tokens_total: int = Field(default=0, ge=0)  # input, cache read and cache write
    # The next four are what the rules read, or what the next call needs.
    stop_details: StopDetails | None = None
    raw_stop_reason: str | None = None
    response_ref: BlobRef | None = None  # the provider's reply, whole
    paused_ref: BlobRef | None = None  # the text so far of a turn that stopped on pause_turn


class ToolExecInput(BaseModel):
    task_id: str
    turn_index: int
    call: ToolCallSummary
    pre_sha: str | None  # the checkpoint the call must start from
    sandbox_id: str


class ToolResultSummary(BaseModel):
    """A tool result as append_results gets it: the text it answers with, by ref or inline.

    `output_ref` names the blob that tool_exec stored. A result that no tool made, such as the
    answer to a `finish` without a summary, has nothing stored and carries its short text in
    `output`. Exactly one of the two is set.
    """

    call_id: str
    ok: bool
    output: str | None = None
    output_ref: BlobRef | None = None

    @model_validator(mode="after")
    def _text_is_inline_or_by_ref(self) -> Self:
        if (self.output is None) == (self.output_ref is None):
            raise ValueError("set exactly one of output and output_ref")
        return self


class AppendResultsInput(BaseModel):
    task_id: str
    turn_index: int
    transcript_ref: BlobRef
    results: list[ToolResultSummary]
    # Text from the harness, such as the nudge. It goes after the results, as the API requires.
    user_text: str | None = None


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
    """The text the model gave to `finish`, or a fixed line when it gave none.

    The workflow no longer calls this: rules.run_calls decides what a finish is, and a finish
    with no text is answered with an error and the task goes on. It stays as a way to put a
    line next to a finish call that has none.
    """
    return rules.finish_summary(call.input) or NO_SUMMARY


def summarize_result(result: ToolResult) -> ToolResultSummary:
    """What append_results needs from a result: its blob when tool_exec stored one."""
    if result.output_ref is not None:
        return ToolResultSummary(call_id=result.call_id, ok=result.ok, output_ref=result.output_ref)
    return ToolResultSummary(call_id=result.call_id, ok=result.ok, output=result.output)
