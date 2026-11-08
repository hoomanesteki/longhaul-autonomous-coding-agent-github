"""The shapes the console API returns and accepts (LH-801).

`make api-types` dumps the OpenAPI schema of these models to console/openapi.json, and the
console's typed client is generated from it (LH-802). A change here is therefore a change of the
contract with the console, and the unit test that compares the file with the app fails until the
file is regenerated.

Models that already exist in longhaul.contracts are reused where the console needs the same
thing (a plan, a message, a model reply, the status of a task), so the console sees one shape for
one idea. Money is a float, as in TaskStatus. The database keeps numeric(10, 4), and a float
carries every cent of it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from longhaul.contracts import LLMResponse, Message, Plan, TaskStatus
from longhaul.durable.client import WorkflowState

__all__ = [
    "ActionResult",
    "BudgetRequest",
    "CancelRequest",
    "Health",
    "HintRequest",
    "LiveState",
    "MemoryItemView",
    "PlanView",
    "RejectPlanRequest",
    "RepoView",
    "Signal",
    "TaskDetail",
    "TaskPage",
    "TaskRecord",
    "TaskSummary",
    "TimelineEvent",
    "TimelineItem",
    "TimelinePage",
    "TimelineToolCall",
    "TimelineTurn",
    "ToolOutput",
    "TurnDetail",
]


# Where the workflow of a task stands. "unreachable" is the answer when Temporal does not respond,
# which is not the same as a workflow that does not exist.
LiveState = WorkflowState | Literal["unreachable"]


# ---------- tasks ----------
class TaskSummary(BaseModel):
    """One row of the task list: what the `tasks` table says."""

    id: str
    repo: str
    issue_number: int | None
    title: str
    status: str
    phase: str
    outcome: str | None
    pr_url: str | None
    model_coder: str | None
    cost_usd: float
    turns: int
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime


class TaskRecord(TaskSummary):
    """The whole row, with the settings the task runs with and its context totals."""

    settings: dict[str, Any]
    input_tokens_total: int
    output_tokens_total: int
    cache_read_total: int
    compactions: int
    max_fraction: float


class TaskPage(BaseModel):
    items: list[TaskSummary]
    next_cursor: str | None


class PlanView(BaseModel):
    """One version of the plan of a task, with the decision on it."""

    version: int
    status: Literal["proposed", "approved", "rejected", "superseded"]
    plan: Plan
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime


class TaskDetail(BaseModel):
    """A task as the console's detail page shows it.

    The row is what the activities last wrote, and it can lag behind the task. `live` is the
    answer of the workflow to its status query, so it is current, and it is None when nobody can
    answer: the workflow is gone, Temporal is down, or no worker is running. `workflow` says
    which of these it was.
    """

    task: TaskRecord
    workflow: LiveState
    live: TaskStatus | None
    plan: PlanView | None


# ---------- timeline ----------
class TimelineEvent(BaseModel):
    kind: Literal["event"] = "event"
    ts: datetime
    id: int
    type: str
    payload: dict[str, Any]


class TimelineTurn(BaseModel):
    """One model call. A turn that was asked again has a row for each attempt."""

    kind: Literal["turn"] = "turn"
    ts: datetime
    turn_index: int
    attempt: int
    role: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    stop_reason: str | None
    latency_ms: int | None
    ended_at: datetime | None


class TimelineToolCall(BaseModel):
    """One tool call. `ts` is when its row was written, which is when the call ended."""

    kind: Literal["tool_call"] = "tool_call"
    ts: datetime
    id: str
    turn_index: int
    name: str
    ok: bool
    exit_code: int | None
    duration_ms: int
    has_output: bool


TimelineItem = Annotated[
    TimelineEvent | TimelineTurn | TimelineToolCall, Field(discriminator="kind")
]


class TimelinePage(BaseModel):
    items: list[TimelineItem]
    next_cursor: str | None


# ---------- turns and outputs ----------
class TurnDetail(BaseModel):
    """The request and the reply of one model call, as stored and then redacted.

    `request` is the transcript the call began from. `response` is None for a call that failed
    after it was billed. `attempts` lists every attempt of the turn, and `attempt` is the one
    shown: the last, unless the caller asked for another.
    """

    turn_index: int
    attempt: int
    attempts: list[int]
    role: str
    started_at: datetime
    ended_at: datetime | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    stop_reason: str | None
    latency_ms: int | None
    request: list[Message] | None
    response: LLMResponse | None


class ToolOutput(BaseModel):
    """Lines `start_line` to `end_line` of the full output of a tool call, redacted.

    Line numbers count the redacted text. `next_start` is set when the page ended before the
    range that was asked for did.
    """

    call_id: str
    name: str
    ok: bool
    exit_code: int | None
    text: str
    start_line: int
    end_line: int
    total_lines: int
    next_start: int | None


# ---------- actions ----------
Signal = Literal["approve_plan", "reject_plan", "hint", "add_budget", "cancel"]


class ActionResult(BaseModel):
    """The signal that was sent, and who it was sent as."""

    task_id: str
    signal: Signal
    by: str


class RejectPlanRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=10_000)

    @field_validator("feedback")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _not_blank(value)


class HintRequest(BaseModel):
    # The workflow cuts a hint to 2,000 bytes (agent/hints.py). This limit only keeps a request
    # from carrying a megabyte to the signal.
    text: str = Field(min_length=1, max_length=10_000)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        return _not_blank(value)


class BudgetRequest(BaseModel):
    usd: float = Field(default=0.0, ge=0, le=10_000)
    turns: int = Field(default=0, ge=0, le=100_000)

    @model_validator(mode="after")
    def _adds_something(self) -> BudgetRequest:
        if self.usd == 0 and self.turns == 0:
            raise ValueError("add dollars, turns or both")
        return self


class CancelRequest(BaseModel):
    reason: str = Field(default="cancelled from the console", min_length=1, max_length=2_000)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


# ---------- memory ----------
class MemoryItemView(BaseModel):
    """A row of `memory_items` without its vector."""

    id: str
    repo: str
    kind: Literal["repo_map", "command", "convention", "pitfall", "lesson"]
    status: Literal["candidate", "approved", "rejected"]
    source: Literal["default_branch", "merged_pr", "human", "task"]
    source_ref: str | None
    approved_by: str | None
    uses: int
    last_used_at: datetime | None
    created_at: datetime
    content: str


# ---------- repos and health ----------
class RepoView(BaseModel):
    repo: str
    allowlisted: bool
    settings: dict[str, Any]
    tasks: int
    created_at: datetime


class Health(BaseModel):
    status: Literal["ok"]
