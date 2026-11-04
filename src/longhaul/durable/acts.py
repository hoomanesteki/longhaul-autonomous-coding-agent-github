"""What the workflows and their activities agree on: names, payload types and run policies.

TaskWorkflow and PollerWorkflow call every activity by name, with `result_type`, and import none
of the activity modules. Those modules reach the Docker SDK, boto3, the model SDKs and SQLAlchemy,
which invariant I1 keeps out of workflow code. This module has no I/O, so both sides can
import it. The workflow reads the types and the policies. An activity registers under
`Act.name` and takes the input type as its parameter, so the two cannot drift apart.

The policies are the ones in plan section 3.7. Two activities are not in that table and got
their numbers here: `append_results`, which reads and writes one blob, and `cleanup`. Both are
sized like `load_memory`. `ensure_sandbox` is in the table with 10 minutes and 5 attempts, and
has a heartbeat timeout like `prepare_workspace`: it builds a container and runs the setup
command, so a cancel has to reach it and a killed worker has to be noticed in a minute.
`bundle_checkpoint` has the table's 5 minutes and 3 attempts and the same kind of heartbeat
timeout, because it uploads the whole history of a repository. `record_event` is a local
activity: it is one row insert, and it runs inside the worker that holds the workflow. `compact`
has the table's 10 minutes and 5 attempts, and it is a model call, so it gets the backoff, the
non-retryable error kinds and the heartbeat timeout of `llm_turn`.

`poll_once` belongs to PollerWorkflow and is not in the table either (LH-307). A poll reads a
page or two of issues for each repo and starts the tasks that are new, so 3 minutes is generous.
It has a heartbeat timeout because the Schedule skips a tick while a poll is open (ADR-0008): a
poll on a killed worker must end in 30 seconds and not in 3 minutes. Its backoff is short
because the next tick is a retry as well. Whatever a poll did before it failed is on record
(a started task has its row), so a retried poll carries on where the last one stopped.

The fields of the payloads follow section 3.7 as well. LH-204 implemented `llm_turn`,
`tool_exec` and `append_results` against them and added fields. The workflow reads only the
ones it uses.

What crosses an activity boundary stays small (ADR-0005, invariants I2):

- A model turn returns refs to its transcript and its reply, and a summary of each tool call:
  the arguments inline up to INLINE_LIMIT_BYTES for one call and INLINE_ARGS_TOTAL_BYTES for
  the whole reply, and by `args_ref` beyond that.
- A tool call returns a `ToolResult` whose `output` is a preview of at most
  OUTPUT_PREVIEW_CHARS and whose `output_ref` holds the whole text. The model reads that text
  when it is within `tool_output_max_tokens`, and its first and last lines with a handle when it
  is longer (LH-402). `append_results` makes that cut from the blob.
- `append_results` takes one `ToolResultSummary` for each result, which names that blob. A turn
  of fifty calls with 15 KB of output each therefore adds a few KB to the history, and not
  750 KB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from types import NoneType
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy

from longhaul.agent import rules
from longhaul.context.tokens import Decision
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

# The turns one run of TaskWorkflow takes before it hands over to a new run (Continue-As-New).
# The workflow cannot read the config file, so the number is copied from
# `loop.continue_as_new_every_turns`. A unit test keeps the two equal. A turn with one tool call
# adds 18 history events and one with three calls adds 30, so 40 turns come to 720 to 1,200, well
# under the 4,096 at which the server suggests a hand-over (ADR-0004, appendix section 4). That
# suggestion is the safety net for a turn with many calls.
CONTINUE_AS_NEW_EVERY_TURNS = 40

# F6: when the window is too full to wait, or the provider says the prompt is too long, the view
# is compacted down to this many recent turns and the notes. A proactive compaction keeps
# `context.keep_recent_turns`, which the workflow cannot read, so the activity reads it.
FORCED_KEEP_RECENT_TURNS = 2

# The workflow memo key under which a run carries its Tuning.
TUNING_MEMO_KEY = "tuning"
# The workflow memo key under which a run carries the BlobRef of its script. The scripted
# provider runs in the worker, and a worker is started once for many tasks, so the script has to
# travel with the task. `longhaul run --script` uploads it and the workflow hands the ref to every
# model turn (LlmTurnInput.script_ref). Like the tuning, it works for a worker in another process,
# survives a replay and carries over Continue-As-New.
SCRIPT_MEMO_KEY = "script"

# The error type of a tool call that found its sandbox gone. tool_exec raises it as a
# non-retryable ApplicationError, because asking again finds the same empty place. The workflow
# sees the type in the cause of the ActivityError, recreates the sandbox with ensure_sandbox and
# runs the call again from the same pre_sha.
SANDBOX_GONE = "SandboxGoneError"
# How often one tool call may find its sandbox gone, and be run again in a new one, before the
# run ends with an error. A sandbox that dies every time, because the call kills it or because
# the host is failing, must not make the workflow loop.
MAX_SANDBOX_RECREATIONS = 2

# The error types that poll_once raises as ApplicationError. The retry policy names them, so
# the activity and the policy cannot drift apart. The two GitHub names are the ones that
# github/side_effects.py gives its errors.
GITHUB_FATAL = "GitHubFatal"
GITHUB_RETRYABLE = "GitHubRetryable"
# A poll that cannot work with the settings it has: the worker has no GitHub token, or the
# allowlist is not a valid one (github/intake.py). Asking again finds the same settings.
POLLER_CONFIG = "PollerConfigError"

# The Temporal Schedule of the poller (ADR-0008). `longhaul poller install` makes it and the
# runs it starts are named "<schedule id>-run" and a time.
POLLER_SCHEDULE_ID = "longhaul-poller"
# The longest one run of PollerWorkflow may last. All attempts of poll_once at their longest
# come to 3 x 180 s and two waits of 5 s and 10 s, which is under 10 minutes. A run that lasts
# longer has lost its worker, and the Schedule skips ticks for as long as a run is open.
POLLER_RUN_TIMEOUT = timedelta(minutes=15)
# What the Schedule does about ticks it missed while the server was away: it catches up on those
# of the last two minutes and drops the rest. A poll is a full scan, so one poll after an outage
# is all that is needed.
POLLER_CATCHUP_WINDOW = timedelta(minutes=2)


# ---------- payloads ----------
class PrepareWorkspaceOutput(BaseModel):
    sandbox_id: str
    base_sha: str
    head_sha: str


class EnsureSandboxInput(BaseModel):
    """What ensure_sandbox needs to bring a task's sandbox back.

    `head_sha` is the latest checkpoint the workflow knows, and the new workspace is put exactly
    there. `sandbox_id` is the container the workflow thinks it has, or None when it has none.
    The spec is there for the setup command, which runs again in the new container.
    """

    task_id: str
    head_sha: str
    sandbox_id: str | None
    spec: TaskSpec


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
    # The notes reach the model in the first message of the view, where compaction put them
    # (context/compact.py apply), so the system prompt does not change with them (D7). This field
    # is the workflow's copy, which it hands back to the compact activity. The plan and the memory
    # block reach the model in Phases 5 and 6.
    notes: ProgressNotes | None = None
    plan: Plan | None = None
    memory_ref: BlobRef | None = None
    # Every hint of the task, "<by>: <text>", in the order they came. The activity shows the model
    # the ones that are not in the transcript yet (turns.show_hints). The pause and hint flow of
    # LH-506 builds on this.
    hints: list[str] = Field(default_factory=list)
    # D8: the one retry of a turn that stopped on max_tokens runs at the higher cap. The
    # activity maps this to the number in the config, which the workflow cannot read.
    high_output_cap: bool = False
    # The text of a reply that stopped on pause_turn, for the call that continues it.
    paused_ref: BlobRef | None = None
    # The script that answers this task instead of the provider of the worker. It comes from the
    # memo (SCRIPT_MEMO_KEY) and is the same on every turn of a task.
    script_ref: BlobRef | None = None


class LlmTurnOutput(BaseModel):
    """What one model call came to.

    `transcript_ref` is the transcript to carry on from. A reply that is complete (end_turn or
    tool_use) is appended to it. Any other reply is not stored, so the ref is the input's, or the
    first message alone when the turn had no transcript yet. The workflow decides what to do
    next from `stop_reason`, with the rules in longhaul.agent.rules.

    The workflow cannot count tokens, so the activity does it (LH-404). `request_tokens` is the
    projection of the request this call sent, and `context_decision` says what the next turn
    should begin with, from the projection of the request after it: "ok" to go on, "compact" to
    compact the view first, "force" to compact down to FORCED_KEEP_RECENT_TURNS turns.
    """

    transcript_ref: BlobRef
    tool_calls_ref: BlobRef | None = None  # every call with its full arguments
    tool_call_summaries: list[ToolCallSummary] = Field(default_factory=list)
    stop_reason: StopReason
    usage: Usage
    # What the call cost, as the cost ledger priced it. 0.0 when the activity has no ledger.
    cost_usd: float = Field(default=0.0, ge=0)
    input_tokens_total: int = Field(default=0, ge=0)  # input, cache read and cache write
    # The next four are what the rules read, or what the next call needs.
    stop_details: StopDetails | None = None
    raw_stop_reason: str | None = None
    response_ref: BlobRef | None = None  # the provider's reply, whole
    paused_ref: BlobRef | None = None  # the text so far of a turn that stopped on pause_turn
    context_decision: Decision = "ok"
    request_tokens: int = Field(default=0, ge=0)  # 0 when the activity has no context policy


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


# What a compaction came to. The first three are context.compact.CompactStatus, and a test keeps
# them equal. "unchanged" is the activity's own: there was no turn old enough to fold.
CompactStatus = Literal["ok", "repaired", "fallback", "unchanged"]


class CompactInput(BaseModel):
    """What the compact activity needs to fold the older turns of a view into notes.

    `turn_index` is the turn that asked for it, for the blob key and the ledger rows. The spec
    and the role build the system prompt, so the activity can say how many tokens the request
    took before and after. `forced` picks FORCED_KEEP_RECENT_TURNS and not the configured number.
    """

    task_id: str
    turn_index: int
    role: Role
    spec: TaskSpec
    transcript_ref: BlobRef
    notes: ProgressNotes | None = None
    forced: bool = False
    # The script of the task, as in LlmTurnInput. The compactor is scripted in a scripted run.
    script_ref: BlobRef | None = None


class CompactOutput(BaseModel):
    """The new view and the notes that belong to it.

    `notes` are the notes to carry from now on. For a fallback they are the previous ones, and
    for "unchanged" the input's. The tokens are those of the whole request (system prompt, view
    and tools) before and after. `cost_usd` is what the compactor's calls cost on the ledger.
    """

    transcript_ref: BlobRef
    notes: ProgressNotes | None
    status: CompactStatus
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    folded_turns: int = Field(ge=0)  # the turns that this compaction removed from the view
    cost_usd: float = Field(default=0.0, ge=0)


class CleanupInput(BaseModel):
    task_id: str
    # None when the workflow stopped before it learned the id. The activity then finds the
    # task's containers by their task label.
    sandbox_id: str | None
    reason: str


class RecordEventInput(BaseModel):
    """One row for the `events` table. The workflow cannot write to Postgres itself."""

    task_id: str
    type: str  # one of the event types in plan section 3.5
    payload: dict[str, Any] = Field(default_factory=dict)  # JSON, and small


class BundleCheckpointInput(BaseModel):
    """What bundle_checkpoint needs: the task, and the turn that has just ended.

    The bundle holds the branch head, and the `checkpoints` row that names it is filed under
    this turn.
    """

    task_id: str
    turn_index: int


class PollResult(BaseModel):
    """What one poll came to, as counts. The task ids are in the log and in Temporal.

    `repos` is the allowlisted repos it looked at, `issues` the open issues it saw with the label,
    `started` the tasks it started, and `known` the issues it left alone because their task
    already has a row or a workflow.
    """

    repos: int = Field(default=0, ge=0)
    issues: int = Field(default=0, ge=0)
    started: int = Field(default=0, ge=0)
    known: int = Field(default=0, ge=0)


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
    # A local activity runs in the worker that holds the workflow, with no task queue round trip
    # and no heartbeat. It is for a call of a few seconds that should add no server-side task,
    # such as a log row. The workflow starts it with local_options().
    local: bool = False


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

ENSURE_SANDBOX = Act(
    "ensure_sandbox",
    EnsureSandboxInput,
    str,  # the id of the sandbox: the one it was given when that runs, else a new one
    start_to_close=timedelta(minutes=10),
    heartbeat=timedelta(seconds=60),
    retry=RetryPolicy(maximum_attempts=5, non_retryable_error_types=[_OVERSIZED]),
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

BUNDLE_CHECKPOINT = Act(
    "bundle_checkpoint",
    BundleCheckpointInput,
    BlobRef,
    start_to_close=timedelta(minutes=5),
    heartbeat=timedelta(seconds=60),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

RECORD_EVENT = Act(
    "record_event",
    RecordEventInput,
    NoneType,
    start_to_close=timedelta(seconds=10),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
    local=True,
)

# The one activity of PollerWorkflow. It takes no input: the repos, the label and the task queue
# are the worker's settings.
POLL_ONCE = Act(
    "poll_once",
    NoneType,
    PollResult,
    start_to_close=timedelta(minutes=3),
    heartbeat=timedelta(seconds=30),
    retry=RetryPolicy(
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=30),
        maximum_attempts=3,
        non_retryable_error_types=[_OVERSIZED, GITHUB_FATAL, POLLER_CONFIG],
    ),
)

COMPACT = Act(
    "compact",
    CompactInput,
    CompactOutput,
    start_to_close=timedelta(minutes=10),
    # Section 3.7 gives no heartbeat. It is a model call like llm_turn, so a cancel has to reach
    # it and a killed worker has to be noticed in half a minute.
    heartbeat=timedelta(seconds=30),
    retry=RetryPolicy(
        initial_interval=timedelta(seconds=2),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=120),
        maximum_attempts=5,
        non_retryable_error_types=[
            ProviderErrorKind.INVALID_REQUEST.value,
            ProviderErrorKind.AUTH.value,
            ProviderErrorKind.CONTEXT_TOO_LONG.value,
            _OVERSIZED,
        ],
    ),
)

ALL: tuple[Act[Any, Any], ...] = (
    PREPARE_WORKSPACE,
    ENSURE_SANDBOX,
    LLM_TURN,
    TOOL_EXEC,
    APPEND_RESULTS,
    CLEANUP,
    BUNDLE_CHECKPOINT,
    RECORD_EVENT,
    POLL_ONCE,
    COMPACT,
)


# ---------- per-run tuning ----------
class Tuning(BaseModel):
    """Per-run changes to the section 3.7 timeouts, for tests and chaos runs.

    After a hard kill the server notices a dead activity only when its heartbeat timeout
    passes: up to 60 s for `prepare_workspace` and `ensure_sandbox`, 30 s for `llm_turn`, 15 s
    for `tool_exec` (ADR-0004 section 3). A chaos run that kills the worker five times cannot
    wait that long, so it starts its tasks with a shorter heartbeat timeout.

    The value travels in the workflow memo, not in a global, so it works for a worker in
    another process, survives a replay and carries over Continue-As-New. A production run
    has no tuning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The server fires the timeout on its own clock, so the activity has to heartbeat well
    # inside it. The Python SDK sends at most one heartbeat per 0.8 timeouts.
    heartbeat_timeout_s: float | None = Field(default=None, gt=0)
    # A test or a chaos run cannot wait for 40 turns to see a hand-over, so it sets a small
    # number. Every run of the chain then hands over after that many turns, because the memo
    # carries over Continue-As-New.
    continue_as_new_every_turns: int | None = Field(default=None, ge=1)


def options(act: Act[Any, Any], tuning: Tuning | None = None) -> workflow.ActivityConfig:
    """The keyword arguments for workflow.start_activity: timeouts, retries and cancellation.

    When the workflow cancels an activity, it waits until the activity has stopped
    (WAIT_CANCELLATION_COMPLETED). The clean-up that follows removes the sandbox and the
    volume, and an activity that is still running, a `prepare_workspace` in the middle of a
    clone for one, could create them again after the sweep and leave them behind. The SDK's
    default (TRY_CANCEL) does not wait.

    The activity learns about the cancel at its next heartbeat, and the SDK sends at most one
    heartbeat per 0.8 timeouts. A cancel can therefore take up to 48 s while the workspace is
    prepared or the sandbox is recreated, 24 s during a model turn and 12 s during a tool call.
    If the worker is gone, the heartbeat timeout ends the wait. An activity without a heartbeat
    timeout cannot be interrupted, so only short ones have none.

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


def local_options(act: Act[Any, Any]) -> workflow.LocalActivityConfig:
    """The keyword arguments for workflow.start_local_activity.

    A local activity has no heartbeat, so a cancel reaches it only when it ends. That is why
    only a call of a few seconds is local.
    """
    return {
        "start_to_close_timeout": act.start_to_close,
        "retry_policy": act.retry,
        "cancellation_type": workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    }


def continue_as_new_every(tuning: Tuning | None = None) -> int:
    """The turns between two hand-overs: the run's tuning, or the configured number."""
    if tuning is not None and tuning.continue_as_new_every_turns is not None:
        return tuning.continue_as_new_every_turns
    return CONTINUE_AS_NEW_EVERY_TURNS


def hand_off_due(turn_index: int, tuning: Tuning | None = None, *, suggested: bool = False) -> bool:
    """True when the run should hand over to a new one after the turn that has just ended.

    `turn_index` counts the turns done so far, so it is 1 after the first. `suggested` is the
    server's own advice, workflow.info().is_continue_as_new_suggested().
    """
    return suggested or (turn_index > 0 and turn_index % continue_as_new_every(tuning) == 0)


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
