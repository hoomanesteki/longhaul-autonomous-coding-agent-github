"""What the workflows and their activities agree on: names, payload types and run policies.

TaskWorkflow and PollerWorkflow call every activity by name, with `result_type`, and import none
of the activity modules. Those modules reach the Docker SDK, boto3, the model SDKs and SQLAlchemy,
which invariant I1 keeps out of workflow code. This module has no I/O, so both sides can
import it. The workflow reads the types and the policies. An activity registers under
`Act.name` and takes the input type as its parameter, so the two cannot drift apart.

The policies are the ones in plan section 3.7. Two activities are not in that table and got
their numbers here: `append_results`, which reads and writes one blob, and `cleanup`. Both are
sized like `load_memory`, which has the table's 2 minutes and 3 attempts and no heartbeat: it is
one search of the store and one blob write. `ensure_sandbox` is in the table with 10 minutes and
5 attempts, and has a heartbeat timeout like `prepare_workspace`: it builds a container and runs
the setup command, so a cancel has to reach it and a killed worker has to be noticed in a minute.
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

The GitHub activities (LH-306) are in `GITHUB`, and not in `ALL`, because only a worker with a
GitHub token registers them. `post_comment`, `add_label`, `remove_label` and `create_pr` are the
idempotent writes of github/side_effects.py with the policies of section 3.7: 2 minutes and 10
attempts, backing off to 5 minutes. The workflow calls the first three itself, for the labels and
the pickup comment (github/lifecycle.py). It does not call `create_pr`: `deliver` makes the pull
request inside the activity with the plain function, after the push, because the body is built
there from the diff and from the test output, and a body over 16 KB may not cross an activity
boundary (CONTRIBUTING.md rule 5). `build_pr_branch` has the 5 minutes and 3 attempts of section
3.7. `deliver` is not in the table. It pushes a branch, whose git timeout is 10 minutes, and then
does what create_pr does, so it gets 10 minutes, a heartbeat that a killed worker is noticed by in
a minute, and 5 attempts with the backoff of the writes.

The planner (LH-501) adds two activities that are not in the table. `store_plan` writes one row
of the `plans` table, so it is sized like `append_results` and every worker registers it.
`post_plan` renders the plan as a checklist from a template and posts it with the idempotent
`post_comment` of github/side_effects.py, under the key `<task>:plan_comment:<version>`. It is a
GitHub write with the policy of `post_comment`, and it sits in `GITHUB` with them. The template is
read inside the activity, so the workflow, which may not read files, passes the plan itself.

The coder with a plan (LH-503) adds one more, `record_step`. It writes the event of a step update
and, for a step that is done, the `step` checkpoint at the head of the work. Like `store_plan` it
is a database write of a row or two, sized like `append_results`, and every worker registers it.
The plan gate (LH-502) adds `decide_plan`, which records who approved or rejected a version of the
plan on its row of the `plans` table. It is one row update, so it is sized like `store_plan` and
every worker registers it.

The pause for a person (LH-506) adds `summarize_for_human`. It is a compactor model call and a
template, so it has the numbers of `compact`: 10 minutes, 5 attempts, the backoff and the
non-retryable error kinds of a model call, and a heartbeat timeout. It writes no row but the ledger
rows of its calls. The comment itself is posted by `post_comment`, so every worker registers the
first and only a worker with a token the second.

The run report (LH-705) adds `write_run_report`, which is in the table. It reads the tables of the
task and writes one blob, so every worker registers it. The workflow does not call it yet:
`deliver` makes the report for the body of the pull request, and a later wind-up can call the
activity to keep a final copy.

The lessons of a finished task (LH-605) add `extract_lessons`. It is a compactor model call and a
few rows of the memory table, so it has the model-call policy of `summarize_for_human` with 3
attempts: a task has ended when it runs, and its lessons are a bonus that a worker can give up on.
It sits in `ALL` because the workflow names it on any worker that keeps memory.

The budgets (LH-702) add two. `check_budget` is in section 3.7 with 30 seconds and 3 attempts: two
sums over the ledger and some arithmetic, which every worker registers. `post_budget` renders the
comment that says why a budget stopped the task and posts it under the key `<task>:budget:<n>`. It
is a `post_comment` with a rendered body, so it has that policy and sits in `GITHUB`.

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
from datetime import datetime, timedelta
from types import NoneType
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from temporalio import workflow
from temporalio.common import RetryPolicy

from longhaul.agent import rules
from longhaul.context.modes import ContextMode
from longhaul.context.tokens import Decision
from longhaul.contracts import (
    BlobRef,
    Budgets,
    CarryState,
    Outcome,
    Plan,
    ProgressNotes,
    ProviderErrorKind,
    Role,
    StopDetails,
    StopReason,
    StuckReason,
    TaskSpec,
    TaskStatus,
    ToolResult,
    Usage,
)
from longhaul.cost.budget import BudgetNumbers, BudgetReason, BudgetVerdict
from longhaul.durable.payload_guard import PayloadTooLargeError
from longhaul.github.inputs import CommentInput, LabelInput, PullRequestInput
from longhaul.roles.stuck import CallSignal

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
# How long a hand-over waits for a status comment that is still being posted (LH-308). A comment
# that GitHub accepts takes a second or two, so this only runs out when the post keeps retrying.
# The comment is then dropped with the run, and a cancel ends the wait at once.
STATUS_GRACE = timedelta(seconds=30)

# F6: when the window is too full to wait, or the provider says the prompt is too long, the view
# is compacted down to this many recent turns and the notes. A proactive compaction keeps
# `context.keep_recent_turns`, which the workflow cannot read, so the activity reads it.
FORCED_KEEP_RECENT_TURNS = 2

# The turns the planner gets to submit its plan (LH-501). The workflow cannot read the config
# file, so the number is copied from `loop.planner_max_turns`. A unit test keeps the two equal.
PLANNER_MAX_TURNS = 20

# The hours a task waits for a person before it gives up, the same number as
# `stuck.hint_wait_hours` of the config. The plan gate (LH-502) waits this long for an approval,
# and a paused task (LH-506) this long for a hint, a budget top-up or a cancel before it ends as
# stuck. The workflow cannot read the config file, so the number is copied here, a unit test keeps
# the two equal, and a worker whose config holds another number stops before it takes a task
# (durable/worker.py).
HINT_WAIT_HOURS = 24

# The failed reviews after which a task ends as stuck (LH-504). The workflow cannot read the config
# file, so the number is copied from `loop.max_verify_rounds`. A unit test keeps the two equal.
MAX_VERIFY_ROUNDS = 3

# The workflow memo key under which a run carries its Tuning.
TUNING_MEMO_KEY = "tuning"
# The workflow memo key that switches planning on for a task (LH-501). A task started with
# `start_task(plan=True)` runs the planner before the coder, and one started without it goes
# straight to coding, as every task did before the planner existed. It is in the memo for the
# reasons the tuning and the script are: it reaches a worker in another process, survives a
# replay and carries over Continue-As-New, so the plan is not made again after a hand-over.
PLAN_MEMO_KEY = "plan"
# The workflow memo key under which a run carries the BlobRef of its script. The scripted
# provider runs in the worker, and a worker is started once for many tasks, so the script has to
# travel with the task. `longhaul run --script` uploads it and the workflow hands the ref to every
# model turn (LlmTurnInput.script_ref). Like the tuning, it works for a worker in another process,
# survives a replay and carries over Continue-As-New.
SCRIPT_MEMO_KEY = "script"
# The workflow memo key under which a run carries the context mode it asked for (`longhaul run
# --context-mode`, LH-406). A run that names none has no key, and the context.mode of the worker's
# config decides. The workflow hands the mode to every llm_turn and compact, which hold the config.
# Like the script, it works for a worker in another process, survives a replay and carries over
# Continue-As-New.
CONTEXT_MODE_MEMO_KEY = "context_mode"

# The `type` of the record_event call that a run makes when it ends (LH-704). The worker counts
# the task from it: the workflow cannot, because counting is I/O. A hand-over to a new run
# (Continue-As-New) is not an end and makes no such call. The payload holds `kind` (an
# OutcomeKind value), `repo`, `turns`, `cost_usd` and a `summary` cut to OUTCOME_SUMMARY_CHARS.
OUTCOME_EVENT = "outcome"
OUTCOME_SUMMARY_CHARS = 500
# The workflow memo key under which a run carries the count of the status comments it has posted
# (LH-308). The key of a status comment is its number, and a run that began counting at 1 again
# after a hand-over would reuse the key of an earlier comment that had other text. CarryState is a
# contract and has no field for it, and the memo is carried over Continue-As-New like the tuning.
STATUS_MEMO_KEY = "status_posts"
# The workflow memo key that has a stuck task open a draft pull request with its partial work
# (LH-507, the repo setting `open_draft_on_stuck`). The poller sets it from the repo entry of the
# config when it starts the task, because the workflow cannot read the config and RepoSettings is
# a contract that this story does not change. A task started without it opens no such draft.
OPEN_DRAFT_MEMO_KEY = "open_draft_on_stuck"

# How a task ends (LH-507). The purpose part of the idempotency key of the final comment, which
# is `<task>:final:1`: a task ends once, so there is one such comment.
FINAL_COMMENT = "final"
# The `type` of the record_event call that keeps the trace of an exception that ended a task. The
# payload holds the error `type` and the `trace`, cut to ERROR_TRACE_CHARS. The cut leaves the
# event far under the 16 KB that CONTRIBUTING.md rule 5 allows across an activity boundary even
# when every character is four bytes of JSON.
ERROR_EVENT = "error"
ERROR_TRACE_CHARS = 3000
# The `type` of the record_event call that keeps the lessons drafted for a finished task (LH-605),
# for the run report. The payload holds the `status`, the `source_ref` (the pull request number or
# the task id), the `count` and the `items`, each with its `kind` and its `content`.
LESSONS_EVENT = "lessons"
# How long the bare repo of a task that has ended is kept. The checkpoints in it are how a person
# looks at what a stuck or failed task did, and the janitor (clean_repos) deletes the repo after
# this long. A repo whose task has no outcome belongs to a task that is still running and is
# never deleted.
REPO_RETENTION = timedelta(days=7)

# The error type of a tool call that found its sandbox gone. tool_exec raises it as a
# non-retryable ApplicationError, because asking again finds the same empty place. The workflow
# sees the type in the cause of the ActivityError, recreates the sandbox with ensure_sandbox and
# runs the call again from the same pre_sha.
SANDBOX_GONE = "SandboxGoneError"
# How often one tool call may find its sandbox gone, and be run again in a new one, before the
# run ends with an error. A sandbox that dies every time, because the call kills it or because
# the host is failing, must not make the workflow loop.
MAX_SANDBOX_RECREATIONS = 2

# The error types that the GitHub activities (poll_once and the writes) raise as
# ApplicationError. The retry policies name them, so the activity and the policy cannot drift
# apart. A unit test keeps the first two equal to the names of the client's exceptions, which the
# workflow cannot import.
GITHUB_FATAL = "GitHubFatal"
GITHUB_RETRYABLE = "GitHubRetryable"
# A poll that cannot work with the settings it has: the worker has no GitHub token, or the
# allowlist is not a valid one (github/intake.py). Asking again finds the same settings.
POLLER_CONFIG = "PollerConfigError"

# The Temporal Schedule of the poller (ADR-0008). `longhaul poller install` makes it and the
# runs it starts are named "<schedule id>-run" and a time.
POLLER_SCHEDULE_ID = "longhaul-poller"
# The longest one run of PollerWorkflow may last. All attempts of poll_once at their longest
# come to 3 x 180 s and two waits of 5 s and 10 s, which is under 10 minutes, and the sweep of
# the janitor after it (clean_repos) adds at most 2 x 120 s and a wait. A run that lasts longer
# has lost its worker, and the Schedule skips ticks for as long as a run is open.
POLLER_RUN_TIMEOUT = timedelta(minutes=15)
# What the Schedule does about ticks it missed while the server was away: it catches up on those
# of the last two minutes and drops the rest. A poll is a full scan, so one poll after an outage
# is all that is needed.
POLLER_CATCHUP_WINDOW = timedelta(minutes=2)

# A request to a write that no retry can fix: a missing body, a key that is not valid, a key that
# was used for another effect (github/side_effects.py).
SIDE_EFFECT_ERRORS = ("SideEffectError", "InvalidKeyError", "KeyReuseError")
# The work of the task changes no file, so there is no pull request to make. build_pr_branch
# raises it, and the workflow ends the task with an error outcome. A unit test keeps the name
# equal to checkpoints.squash.NothingToDeliverError.
NOTHING_TO_DELIVER = "NothingToDeliverError"
# A push to the default branch or a protected name (failure mode F15). Never retried.
FORBIDDEN_BRANCH = "ForbiddenBranchError"


# ---------- payloads ----------
class RunState(CarryState):
    """CarryState plus what only the workflow carries from run to run (LH-604).

    CarryState is a shared contract (contracts.py) with no field for the memory block, and this
    story does not change it. The workflow takes and hands over this subclass instead, so the
    extra field rides through Continue-As-New with the rest of the state. The JSON of a CarryState
    is a valid RunState with no block, which is how a workflow that an older worker started
    decodes, and the JSON of a RunState is a valid CarryState to a reader that does not know the
    field.

    `memory_ref` is the memory block that load_memory wrote for the task, or None when the task
    has none. It is set once, by the run that prepared the workspace, and never changes.

    The budget fields are those of LH-702. They are in the state and not in the memo because they
    change while the task runs, and the state is what a hand-over carries. A history that began
    before the story has none of them, and the defaults give it the behaviour it recorded.

    - `enforce_budgets` is set when the task starts (`start_task(enforce_budgets=True)`). A task
      without it makes no budget check and ends at its turn limit with ERROR "turn limit", as
      before.
    - `budget_pauses` counts the pauses for a budget, which number the comment `budget:<n>`.
    - `paused_s` is the seconds the task has spent waiting for a person or for midnight. The wall
      clock leaves them out, and a top-up after a wall-clock pause sets it to the whole time the
      task has run, which starts a new window.
    - `last_input_tokens` is the size the next request is expected to have, from the last
      llm_turn (`next_request_tokens`, else the size of the request it sent), and what the
      budget check prices as input. It is 0 again when the transcript is cleared.
    """

    memory_ref: BlobRef | None = None
    enforce_budgets: bool = False
    budget_pauses: int = Field(default=0, ge=0)
    paused_s: float = Field(default=0.0, ge=0)
    last_input_tokens: int = Field(default=0, ge=0)


class PrepareWorkspaceOutput(BaseModel):
    """What the workflow keeps of a prepared workspace.

    `repo_map_ref` is the repo map for the task's issue (LH-602): text in the blob store, for
    the memory block of the prompt (LH-604). It is None when the worker has no memory store, or
    the map could not be built, and the task goes on without it.

    `memory_enabled` says that this worker keeps memory, and so that the workflow should call
    load_memory next. The workflow reads it from the recorded result and not from any setting,
    so the history of a task that began before LH-604 has no such call to replay, and a worker
    without a store adds no activity to its tasks. `test_command` is the first approved test
    command of the repo (retrieve.first_test_command), and is set only when the repo settings
    name none: the workflow puts it into the spec, so every prompt of the task names it.
    """

    sandbox_id: str
    base_sha: str
    head_sha: str
    repo_map_ref: BlobRef | None = None
    memory_enabled: bool = False
    test_command: str | None = None


class LoadMemoryInput(BaseModel):
    """What load_memory needs to write the memory block of a task.

    The spec gives the repo and the issue text that the lessons are searched by. `repo_map_ref`
    is the map that prepare_workspace wrote for this issue, if it wrote one.
    """

    spec: TaskSpec
    repo_map_ref: BlobRef | None = None


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


class CheckRun(BaseModel):
    """One command of the pre-check, or the diff, as the verifier's first message is built from it.

    `command` is the label of the command, cut to a line. `passed` and `exit_code` are what the
    workflow saw: a command passes when it finished with exit code 0, and `exit_code` is None for
    one that did not finish. `output_ref` is the full text that tool_exec stored, which llm_turn
    reads and cuts, so that a long test log never travels through the history. `call_id` names the
    handle the verifier can page the rest with.
    """

    command: str
    call_id: str
    passed: bool
    exit_code: int | None = None
    output_ref: BlobRef | None = None


class ReviewBrief(BaseModel):
    """What the verifier is shown besides the plan: the pre-check, then the diff (LH-504)."""

    checks: list[CheckRun] = Field(default_factory=list)
    diff: CheckRun | None = None


class LlmTurnInput(BaseModel):
    task_id: str
    turn_index: int
    role: Role
    # The system prompt is built from it, in the same bytes on every call of a transcript (D7).
    spec: TaskSpec
    transcript_ref: BlobRef | None  # None before the first turn
    # The notes reach the model in the first message of the view, where compaction put them
    # (context/compact.py apply), so the system prompt does not change with them (D7). This field
    # is the workflow's copy, which it hands back to the compact activity.
    notes: ProgressNotes | None = None
    # The plan with the status of each step, as the workflow holds it. A coder with a plan reads it
    # in the first message of its transcript, which llm_turn brings up to date before each call
    # (LH-503). It also decides the coder's prompt and tool list, so it is set for the whole
    # transcript or for none of it. The other roles ignore it.
    plan: Plan | None = None
    # The memory block that load_memory wrote for the task (LH-604): text in the blob store that
    # ends the system prompt. It is the same ref on every turn of the task, in every run, so the
    # prompt is the same bytes. None means the task has no block.
    memory_ref: BlobRef | None = None
    # Every hint of the task, "<by>: <text>", in the order they came. The activity shows the model
    # the ones that are not in the transcript yet (turns.show_hints). A hint is cut to
    # agent/hints.MAX_HINT_BYTES when it arrives.
    hints: list[str] = Field(default_factory=list)
    # D8: the one retry of a turn that stopped on max_tokens runs at the higher cap. The
    # activity maps this to the number in the config, which the workflow cannot read.
    high_output_cap: bool = False
    # The text of a reply that stopped on pause_turn, for the call that continues it.
    paused_ref: BlobRef | None = None
    # The script that answers this task instead of the provider of the worker. It comes from the
    # memo (SCRIPT_MEMO_KEY) and is the same on every turn of a task.
    script_ref: BlobRef | None = None
    # The context window this run asked for (Tuning.window_tokens), in place of the config's.
    window_tokens: int | None = Field(default=None, ge=1)
    # The context mode this run asked for, from the memo (CONTEXT_MODE_MEMO_KEY). None means the
    # run did not ask, and the config of the worker decides.
    context_mode: ContextMode | None = None
    # What the verifier reads in its first message besides the plan (LH-504). The activity builds
    # that message once, when the transcript starts, so the later turns of a review ignore it. The
    # other roles ignore it always.
    review: ReviewBrief | None = None


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

    `next_request_tokens` is that same projection, with room for the results of the calls just
    made: the size the next request is expected to have. The budget check prices it as input
    (LH-702). It is 0 when the activity has no context policy.
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
    next_request_tokens: int = Field(default=0, ge=0)  # 0 when there is no context policy


class ToolExecInput(BaseModel):
    task_id: str
    turn_index: int
    call: ToolCallSummary
    pre_sha: str | None  # the checkpoint the call must start from
    sandbox_id: str
    # A call of the planning phase (LH-501). The activity runs it, and then puts the tree back on
    # `pre_sha` if it changed anything, and saves no checkpoint. The default is the coder's call.
    read_only: bool = False
    # The context window of the run (Tuning.window_tokens). A tool pages its output to the cap for
    # one result, and that cap shrinks with a small window (context.tokens.result_cap).
    window_tokens: int | None = Field(default=None, ge=1)


class ToolExecResult(ToolResult):
    """What tool_exec returns: the ToolResult, and the facts the stuck detector reads (LH-505).

    `signal` is None for a call that has none to give, such as a read, and for every call of the
    planning phase, which the detector does not watch. The facts are small by design (see
    roles/stuck.py), so they add a few hundred bytes to the history at most. A result stored
    before this field existed reads back as one without facts.
    """

    signal: CallSignal | None = None


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
    # The context window of the run, as in ToolExecInput: the results are cut to the cap that fits.
    window_tokens: int | None = Field(default=None, ge=1)


# What a compaction came to. The first three are context.compact.CompactStatus, and a test keeps
# them equal. "unchanged" is the activity's own: there was no turn old enough to fold, or the mode
# of the run has no compaction.
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
    # The context mode of the run, as in LlmTurnInput. Only `full` compacts.
    context_mode: ContextMode | None = None
    # The memory block of the task, as in LlmTurnInput. The system prompt that the token counts
    # are made from has it, as the one that the model turn sends does.
    memory_ref: BlobRef | None = None


class CompactOutput(BaseModel):
    """The new view and the notes that belong to it.

    `notes` are the notes to carry from now on. For a fallback they are the previous ones, and
    for "unchanged" the input's. The tokens are those of the whole request (system prompt, view
    and tools) before and after. `cost_usd` is what the compactor's calls cost on the ledger.
    A mode without compaction answers "unchanged" too, without a model call.
    """

    transcript_ref: BlobRef
    notes: ProgressNotes | None
    status: CompactStatus
    tokens_before: int = Field(ge=0)
    tokens_after: int = Field(ge=0)
    folded_turns: int = Field(ge=0)  # the turns that this compaction removed from the view
    cost_usd: float = Field(default=0.0, ge=0)


# What the question for a person came to. The three are human_summary.SummaryStatus, and a test
# keeps them equal.
SummaryStatus = Literal["ok", "repaired", "fallback"]


class SummarizeInput(BaseModel):
    """What summarize_for_human needs to write the question for a person (LH-506).

    `transcript_ref` is the view of the coder's transcript, None before the first turn has
    stored one. `reason` and `evidence` are those of the StuckSignal; for AGENT_ASKED the one line
    of evidence is the question the coder asked. `pause` is the number of this pause, counted from
    1, and goes into the title of the comment. `turn_index` is the turn that ended in the signal,
    for the ledger rows.
    """

    task_id: str
    turn_index: int
    spec: TaskSpec
    transcript_ref: BlobRef | None
    notes: ProgressNotes | None = None
    reason: StuckReason
    evidence: list[str] = Field(default_factory=list)
    pause: int = Field(ge=1)
    # The script of the task, as in LlmTurnInput. The compactor is scripted in a scripted run.
    script_ref: BlobRef | None = None


class SummarizeOutput(BaseModel):
    """The comment to show a person, and the question in it.

    `comment` is the whole text for the issue, under 250 words. A local task has no issue, so the
    workflow records it in the `paused` event. `cost_usd` is what the compactor's calls cost on
    the ledger.
    """

    comment: str
    question: str
    status: SummaryStatus
    cost_usd: float = Field(default=0.0, ge=0)


LessonKindName = Literal["lesson", "pitfall", "convention"]
LessonsStatus = Literal["ok", "unusable", "skipped"]


class ExtractLessonsInput(BaseModel):
    """What extract_lessons needs to draft the lessons of a finished task (LH-605).

    `plan` and `notes` are the last ones the workflow holds, and `outcome` is how the task ended,
    with the pull request link when there is one. The stuck events and the verifier's issues are
    not here: they are in the events table, and the activity reads them. `turn_index` is the
    ledger row's turn. The script lets a scripted run answer the compactor, as in
    SummarizeInput.
    """

    task_id: str
    turn_index: int = Field(ge=0)
    spec: TaskSpec
    outcome: Outcome
    plan: Plan | None = None
    notes: ProgressNotes | None = None
    script_ref: BlobRef | None = None


class LessonNote(BaseModel):
    """One lesson as the workflow records it in the `lessons` event."""

    kind: LessonKindName
    content: str


class ExtractLessonsOutput(BaseModel):
    """What extract_lessons did.

    `status` is "ok" when the model answered (and `items` may be empty), "unusable" when its
    answer held no call to read, and "skipped" when the worker has no store or the task left
    nothing to learn from, in which case no model was called. `items` are the notes that were
    stored as candidates, and `source_ref` is the pull request number or the task id they carry.
    """

    status: LessonsStatus
    source_ref: str
    items: list[LessonNote] = Field(default_factory=list)
    cost_usd: float = Field(default=0.0, ge=0)

    @property
    def stored(self) -> int:
        return len(self.items)


class CheckBudgetInput(BaseModel):
    """What check_budget needs to say whether the next model call fits the budgets (LH-702).

    The activity reads the sums from the ledger, and prices the next call with the model of
    `role`: the workflow cannot read the config, so it names the role and the activity finds the
    model and the output cap. `limits` are those of the task, `extra_usd` and `extra_turns` the
    top-ups it has had. `spent_task_usd` is the workflow's running total, which the activity uses
    when the ledger says less, as a worker with no database does. `now` is `workflow.now()`: the
    day of the repo is judged by the time of the workflow, so a task that waited for midnight
    sees the new day, in a test with a skipped clock as well as in production.

    `high_output_cap` is set for the retry of a reply that stopped at max_tokens, which is sent
    with the larger cap, so the call is priced with that cap and not the usual one.

    `last_input_tokens` is 0 for the first request of a transcript, which has no earlier request
    to measure. The workflow then also sends what that request is made of, and the activity
    prices it: the task for the system prompt, the plan, the ref of the memory block and the
    review brief of a verifier. They stay empty for every later request, so a long history does
    not carry them on each check.
    """

    task_id: str
    repo: str
    role: Role
    turn_index: int = Field(ge=0)
    high_output_cap: bool = False
    limits: Budgets
    extra_usd: float = Field(default=0.0, ge=0)
    extra_turns: int = Field(default=0, ge=0)
    spent_task_usd: float = Field(default=0.0, ge=0)
    last_input_tokens: int = Field(default=0, ge=0)
    spec: TaskSpec | None = None
    plan: Plan | None = None
    memory_ref: BlobRef | None = None
    review: ReviewBrief | None = None
    now: datetime


class PostBudgetInput(BaseModel):
    """What post_budget needs: the numbers of a pause, and the issue to tell.

    The activity renders the comment from this and builds the key `<task>:budget:<pause>` from
    the task id, so a retry and a run after a Continue-As-New send the same one. A limit of the
    task has `wait_s`, how long the task waits for a top-up. The daily limit has `resume_at`,
    when the day of the repo starts again.
    """

    task_id: str
    repo: str
    number: int  # the issue
    pause: int = Field(ge=1)
    reason: BudgetReason
    numbers: BudgetNumbers
    wait_s: float | None = Field(default=None, gt=0)
    resume_at: datetime | None = None


class CleanupInput(BaseModel):
    """What cleanup needs: the task, and whether its bare repo stays.

    A task that has ended on its own keeps its repo for REPO_RETENTION, so it sends
    `keep_repo=True` and the janitor deletes the repo later. The default removes it, which is what
    a workflow from before LH-507 asked for and what a cancel still asks for.
    """

    task_id: str
    # None when the workflow stopped before it learned the id. The activity then finds the
    # task's containers by their task label.
    sandbox_id: str | None
    reason: str
    keep_repo: bool = False


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
    already has a row or a workflow. `commands` is the comment commands (LH-308) that it carried
    out: a signal sent to a task, or an attempt started by `/longhaul retry`. `promoted` and
    `rejected` count the lessons that the poll approved because their pull request merged and
    turned down because it was closed (LH-606).
    """

    repos: int = Field(default=0, ge=0)
    issues: int = Field(default=0, ge=0)
    started: int = Field(default=0, ge=0)
    known: int = Field(default=0, ge=0)
    commands: int = Field(default=0, ge=0)
    promoted: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)


class BuildPrBranchInput(BaseModel):
    """What build_pr_branch needs: the finish summary, which titles the commit, and the two ends.

    `base_sha` is the commit the workspace was prepared at (PrepareWorkspaceOutput.base_sha),
    which the workflow keeps. The activity does not read it from the checkpoint repo, where the
    sandbox could move the `base` tag. `head_sha` is the latest checkpoint the workflow knows.
    The work of the whole task is the change from the base to the head.

    `plan` is the plan with the status of each step, for a task that has one (LH-503). The
    activity makes one commit for each done step from it and from the step checkpoints, and a task
    without a plan gets the single commit it always did.
    """

    task_id: str
    repo: str
    summary: str
    base_sha: str
    head_sha: str
    plan: Plan | None = None


class PrBranchOutput(BaseModel):
    """The pull request branch: its name and head, the commit it sits on and the title.

    `base_branch` is the default branch of the repo as GitHub reported it when the branch was
    built. The pull request goes into it, and keeping it here means that every attempt of
    `deliver` sends the same request.
    """

    branch: str
    head_sha: str
    base_sha: str
    base_branch: str
    title: str


class StorePlanInput(BaseModel):
    """What store_plan needs: a version of the plan of a task, for the `plans` table."""

    task_id: str
    version: int = Field(ge=1)
    plan: Plan


class PostPlanInput(BaseModel):
    """What post_plan needs: the plan, to render as a checklist, and the issue to post it on.

    The idempotency key is built by the activity from the task id and the version, so a retry
    and a run after a Continue-As-New send the same one.
    """

    task_id: str
    repo: str
    number: int  # the issue
    version: int = Field(ge=1)
    plan: Plan


class RecordStepInput(BaseModel):
    """What record_step needs: one step update, and the head of the work it was made at.

    A step that is `done` gets a `step` checkpoint at `head_sha`. Every update, the others
    included, is an event with its note.
    """

    task_id: str
    step_id: str
    status: Literal["todo", "doing", "done", "skipped"]
    note: str = ""
    head_sha: str
    turn_index: int = Field(ge=0)


class PlannedStatus(TaskStatus):
    """What status() returns for a task that has a plan: the status, and the plan with its steps.

    TaskStatus is a contract and has no field for the plan, so this is the same model with one
    more. A client that wants the plan asks for the query by name with the type:
    `handle.query("status", result_type=PlannedStatus)`. The SDK takes the result type from the
    method only when the query is asked for by method, so that form reads a TaskStatus, because
    the extra field is ignored. A task without a plan answers with a plain TaskStatus.
    """

    plan: Plan | None = None


class DecidePlanInput(BaseModel):
    """What decide_plan needs: a version of the plan, and the decision a person made on it.

    `by` is the login that sent the signal. Deciding the same version again changes nothing,
    so a retry of the activity cannot turn a rejection into an approval.
    """

    task_id: str
    version: int = Field(ge=1)
    approved: bool
    by: str


class DeliverInput(BaseModel):
    """What deliver needs: the branch to push, and the facts for the title and the body."""

    task_id: str
    repo: str
    issue_number: int
    summary: str
    branch: str
    base_branch: str
    base_sha: str
    head_sha: str
    title: str
    # The counts the verifier gave when it passed the work (LH-504), or None for work that no
    # verifier passed. A pull request is ready for review exactly when this is set, and its body
    # says "Verified: <this>". It is part of the input, so a retry builds the same request.
    verified_tests: str | None = None
    # True for the draft of a task that stopped before it finished (LH-507). The body then refers
    # to the issue and does not close it. Part of the input, so a retry builds the same body.
    partial: bool = False


class PostFinalInput(BaseModel):
    """What post_final needs: how the task ended, and what the report says about the work.

    The activity renders the comment from this and from the diff stats it reads itself. It builds
    the key from the task id, so a retry and a run after a Continue-As-New send the same one.
    `base_sha` and `head_sha` are the ends of the work, and either is None for a task that never
    made a checkpoint. `draft` says whether the pull request of the outcome, if there is one, is
    still a draft. The verifier (Phase 5) is what makes it ready.
    """

    task_id: str
    repo: str
    number: int  # the issue
    outcome: Outcome
    plan: Plan | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    turns: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    draft: bool = True


class JanitorResult(BaseModel):
    """What one sweep of the janitor did: the task ids whose repos it removed, how many repos it
    left, and how many it could not remove."""

    removed: list[str] = Field(default_factory=list)
    kept: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)


class RunReportInput(BaseModel):
    """What write_run_report needs: the task, and how to call its end if the rows do not say.

    `outcome` is an OutcomeKind value. A task that has already recorded its outcome ignores it.
    The activity returns the BlobRef of the report, never the text (CONTRIBUTING.md rule 5).
    """

    task_id: str
    outcome: str | None = None


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

# What the data converter raises in workflow code when the workflow tries to schedule an activity
# with a payload over the cap. The SDK's answer to an unknown exception is to fail the workflow
# task, and the server then runs the same task again, on the same history, for as long as the
# workflow is open. This list makes the error end the workflow instead, with the message of the
# guard. TaskWorkflow passes it as `failure_exception_types` (ADR-0005), and converts its state
# before a hand-over so that the same holds there.
FAIL_THE_WORKFLOW: tuple[type[BaseException], ...] = (PayloadTooLargeError,)

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
    ToolExecResult,
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

LOAD_MEMORY = Act(
    "load_memory",
    LoadMemoryInput,
    BlobRef,  # the rendered memory block, as text
    start_to_close=timedelta(minutes=2),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
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

SUMMARIZE_FOR_HUMAN = Act(
    "summarize_for_human",
    SummarizeInput,
    SummarizeOutput,
    start_to_close=timedelta(minutes=10),
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

# The lessons of a finished task (LH-605). One compactor call and a few inserts. A worker gives up
# after 3 attempts, and the task has ended on its own all the same.
EXTRACT_LESSONS = Act(
    "extract_lessons",
    ExtractLessonsInput,
    ExtractLessonsOutput,
    start_to_close=timedelta(minutes=5),
    heartbeat=timedelta(seconds=30),
    retry=RetryPolicy(
        initial_interval=timedelta(seconds=2),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(seconds=60),
        maximum_attempts=3,
        non_retryable_error_types=[
            ProviderErrorKind.INVALID_REQUEST.value,
            ProviderErrorKind.AUTH.value,
            ProviderErrorKind.CONTEXT_TOO_LONG.value,
            _OVERSIZED,
        ],
    ),
)

# Backoff for a write to GitHub: the first retry comes quickly, and the wait doubles up to the
# five minutes of section 3.7. GitHub's own advice (a rate limit) replaces it for one attempt.
_GITHUB_ERRORS = [_OVERSIZED, GITHUB_FATAL, *SIDE_EFFECT_ERRORS]


def _github_write(attempts: int, never: list[str]) -> RetryPolicy:
    return RetryPolicy(
        initial_interval=timedelta(seconds=5),
        backoff_coefficient=2.0,
        maximum_interval=timedelta(minutes=5),
        maximum_attempts=attempts,
        non_retryable_error_types=never,
    )


POST_COMMENT = Act(
    "post_comment",
    CommentInput,
    str,  # the id of the comment
    start_to_close=timedelta(minutes=2),
    retry=_github_write(10, _GITHUB_ERRORS),
)

ADD_LABEL = Act(
    "add_label",
    LabelInput,
    str,  # the name of the label
    start_to_close=timedelta(minutes=2),
    retry=_github_write(10, _GITHUB_ERRORS),
)

REMOVE_LABEL = Act(
    "remove_label",
    LabelInput,
    str,
    start_to_close=timedelta(minutes=2),
    retry=_github_write(10, _GITHUB_ERRORS),
)

CREATE_PR = Act(
    "create_pr",
    PullRequestInput,
    str,  # the URL of the pull request
    start_to_close=timedelta(minutes=2),
    retry=_github_write(10, _GITHUB_ERRORS),
)

POST_PLAN = Act(
    "post_plan",
    PostPlanInput,
    str,  # the id of the comment
    start_to_close=timedelta(minutes=2),
    retry=_github_write(10, _GITHUB_ERRORS),
)

BUILD_PR_BRANCH = Act(
    "build_pr_branch",
    BuildPrBranchInput,
    PrBranchOutput,
    start_to_close=timedelta(minutes=5),
    retry=RetryPolicy(
        maximum_attempts=3,
        non_retryable_error_types=[_OVERSIZED, GITHUB_FATAL, NOTHING_TO_DELIVER, FORBIDDEN_BRANCH],
    ),
)

DELIVER = Act(
    "deliver",
    DeliverInput,
    str,  # the URL of the pull request
    start_to_close=timedelta(minutes=10),
    heartbeat=timedelta(seconds=60),
    retry=_github_write(5, [*_GITHUB_ERRORS, FORBIDDEN_BRANCH]),
)

STORE_PLAN = Act(
    "store_plan",
    StorePlanInput,
    NoneType,
    start_to_close=timedelta(minutes=2),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

RECORD_STEP = Act(
    "record_step",
    RecordStepInput,
    NoneType,
    start_to_close=timedelta(minutes=2),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

DECIDE_PLAN = Act(
    "decide_plan",
    DecidePlanInput,
    NoneType,
    start_to_close=timedelta(minutes=2),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

# The budget check before a model turn (LH-702): two sums over the ledger and some arithmetic, so
# section 3.7 gives it 30 seconds and 3 attempts. A model without a price fails it for good.
CHECK_BUDGET = Act(
    "check_budget",
    CheckBudgetInput,
    BudgetVerdict,
    start_to_close=timedelta(seconds=30),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED, "PricingError"]),
)

# The comment on the issue when a budget stops a task (LH-702). It is a post_comment whose body the
# activity renders from the numbers and a template, so it has the policy of post_comment and sits
# in GITHUB with the other writes.
POST_BUDGET = Act(
    "post_budget",
    PostBudgetInput,
    str,  # the id of the comment
    start_to_close=timedelta(minutes=2),
    retry=_github_write(10, _GITHUB_ERRORS),
)

# The report on the issue when a task ends (LH-507). It is a post_comment whose body the activity
# renders from the diff stats and a template, so it has the policy of post_comment and sits in
# GITHUB with the other writes.
POST_FINAL = Act(
    "post_final",
    PostFinalInput,
    str,  # the id of the comment
    start_to_close=timedelta(minutes=2),
    retry=_github_write(10, _GITHUB_ERRORS),
)

# The janitor (LH-507). PollerWorkflow calls it after each poll, so the poller schedule is its
# clock. It lists the bare repos on the disk of the worker, asks the database which of their
# tasks ended more than REPO_RETENTION ago and removes those repos. A sweep that fails is run
# again by the next tick, so it gets two attempts of two minutes, which keeps a poll and a sweep
# at their longest inside POLLER_RUN_TIMEOUT.
CLEAN_REPOS = Act(
    "clean_repos",
    NoneType,
    JanitorResult,
    start_to_close=timedelta(minutes=2),
    retry=RetryPolicy(maximum_attempts=2, non_retryable_error_types=[_OVERSIZED]),
)

# The run report (LH-705). It reads the tables of the task and writes one blob, so it has the
# numbers of section 3.7 (2 minutes, 3 attempts) and no heartbeat, and it needs no GitHub token.
# `deliver` builds the same report inside its own activity to put it in the body of the pull
# request, and `longhaul report` builds it without any activity at all.
WRITE_RUN_REPORT = Act(
    "write_run_report",
    RunReportInput,
    BlobRef,
    start_to_close=timedelta(minutes=2),
    retry=RetryPolicy(maximum_attempts=3, non_retryable_error_types=[_OVERSIZED]),
)

ALL: tuple[Act[Any, Any], ...] = (
    PREPARE_WORKSPACE,
    ENSURE_SANDBOX,
    LOAD_MEMORY,
    LLM_TURN,
    TOOL_EXEC,
    APPEND_RESULTS,
    CLEANUP,
    BUNDLE_CHECKPOINT,
    RECORD_EVENT,
    POLL_ONCE,
    COMPACT,
    SUMMARIZE_FOR_HUMAN,
    EXTRACT_LESSONS,
    STORE_PLAN,
    RECORD_STEP,
    DECIDE_PLAN,
    CLEAN_REPOS,
    WRITE_RUN_REPORT,
    CHECK_BUDGET,
)

# What a worker with a GitHub token registers in addition to ALL (LH-306).
GITHUB: tuple[Act[Any, Any], ...] = (
    POST_COMMENT,
    ADD_LABEL,
    REMOVE_LABEL,
    CREATE_PR,
    BUILD_PR_BRANCH,
    DELIVER,
    POST_PLAN,
    POST_FINAL,
    POST_BUDGET,
)


# ---------- per-run tuning ----------
class Tuning(BaseModel):
    """Per-run changes to the section 3.7 timeouts and to the context window.

    The timeouts are for tests and chaos runs. After a hard kill the server notices a dead
    activity only when its heartbeat timeout passes: up to 60 s for `prepare_workspace` and
    `ensure_sandbox`, 30 s for `llm_turn`, 15 s for `tool_exec` (ADR-0004 section 3). A chaos run
    that kills the worker five times cannot wait that long, so it starts its tasks with a shorter
    heartbeat timeout.

    The window is for a run that has to fill it: `longhaul run --window` (LH-407).

    The value travels in the workflow memo, not in a global, so it works for a worker in
    another process, survives a replay and carries over Continue-As-New. A run that changes
    nothing has no tuning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The server fires the timeout on its own clock, so the activity has to heartbeat well
    # inside it. The Python SDK sends at most one heartbeat per 0.8 timeouts.
    heartbeat_timeout_s: float | None = Field(default=None, gt=0)
    # A test or a chaos run cannot wait for 40 turns to see a hand-over, so it sets a small
    # number. Every run of the chain then hands over after that many turns, because the memo
    # carries over Continue-As-New.
    continue_as_new_every_turns: int | None = Field(default=None, ge=1)
    # The wait of a hand-over for a status comment that cannot be posted. The test server does
    # not skip time while an activity runs, so a test of the drop would wait 30 s without this.
    status_grace_s: float | None = Field(default=None, gt=0)
    # The context window of this run, in tokens, in place of context.window_tokens of the config of
    # the worker. The activities that count tokens read it from their inputs (llm_turn, tool_exec
    # and append_results), and settings.Context.for_window says what it changes. The long-task
    # test needs a window that 62 turns fill, and the config's holds a task of hundreds of them.
    window_tokens: int | None = Field(default=None, ge=1)
    # How long a paused task waits for a hint, in seconds, in place of HINT_WAIT_HOURS. The test
    # server skips a 24 hour timer, but a test against a real server cannot wait for one.
    hint_wait_s: float | None = Field(default=None, gt=0)


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


def status_grace(tuning: Tuning | None = None) -> timedelta:
    """The wait of a hand-over for a status comment: the run's tuning, or STATUS_GRACE."""
    if tuning is not None and tuning.status_grace_s is not None:
        return timedelta(seconds=tuning.status_grace_s)
    return STATUS_GRACE


def hint_wait(tuning: Tuning | None = None) -> timedelta:
    """How long a task waits for a person: the run's tuning, or HINT_WAIT_HOURS.

    The plan gate passes no tuning and always waits HINT_WAIT_HOURS. A pause for a hint passes the
    run's, so that a test against a real server can wait less.
    """
    if tuning is not None and tuning.hint_wait_s is not None:
        return timedelta(seconds=tuning.hint_wait_s)
    return timedelta(hours=HINT_WAIT_HOURS)


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
