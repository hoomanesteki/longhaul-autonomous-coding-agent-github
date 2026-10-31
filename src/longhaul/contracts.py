"""Shared contracts. No I/O; imports only stdlib typing helpers and pydantic.

Changing this module needs a note in the PR and a gatekeeper check. Record
every change in the "Contracts changelog" section of docs/architecture.md.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
)
from pydantic.json_schema import JsonSchemaValue

contracts_version = "2"

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
StopReason = Literal[
    "end_turn",
    "tool_use",
    "max_tokens",
    "stop_sequence",
    "refusal",
    "pause_turn",
    "model_context_window_exceeded",
    "other",
]


# ---------- basic records ----------
class Role(StrEnum):
    PLANNER = "planner"
    CODER = "coder"
    VERIFIER = "verifier"
    COMPACTOR = "compactor"


class BlobRef(BaseModel):
    key: str  # e.g. "tasks/lh-o-r-42/turns/0007/response.json"
    sha256: str
    size: int


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    # Asks the provider for arguments that are checked against input_schema. Providers
    # that support it need additionalProperties: false on every object in the schema.
    strict: bool = False


class ToolCall(BaseModel):
    id: str  # provider tool_use id
    name: str
    input: dict[str, Any]


class ToolResult(BaseModel):
    call_id: str
    ok: bool
    output: str  # what the model sees (possibly truncated)
    output_ref: BlobRef | None = None  # full output when truncated
    exit_code: int | None = None
    duration_ms: int = 0
    pre_sha: str | None = None
    post_sha: str | None = None  # set when the call mutated the workspace


class _TaggedBlock(BaseModel):
    """Base of the message blocks. It keeps the `type` tag in every dump.

    The tag is a default, so exclude_unset and exclude_defaults would drop it, and a block
    without it cannot be read back through the Block union.
    """

    type: str

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # The tag goes first, as in a plain dump, so the bytes of a stored transcript do
        # not depend on the dump flags.
        data: dict[str, Any] = handler(self)
        return {"type": self.type, **{key: value for key, value in data.items() if key != "type"}}

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # A serializer function hides the fields from the serialization-mode schema, which
        # FastAPI uses for response models, and a client generated from it would see "an
        # object with any keys". Describe the block by its fields in both modes.
        plain: Any = {key: value for key, value in core_schema.items() if key != "serialization"}
        return handler(plain)


class TextBlock(_TaggedBlock):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(_TaggedBlock):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(_TaggedBlock):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


Block = Annotated[TextBlock | ToolUseBlock | ToolResultBlock, Field(discriminator="type")]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[Block]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class StopDetails(BaseModel):
    """Why the model stopped, when the provider says. Today only a refusal carries details."""

    category: str | None = None
    explanation: str | None = None


class CallOptions(BaseModel):
    """Settings for one call that a provider maps to its API or ignores.

    effort: reasoning effort. It stays fixed for the life of a transcript.
    force_tool: name of a tool the model must call. claude-sonnet-5-5 rejects a forced
        tool choice, so the Anthropic provider raises INVALID_REQUEST instead of sending
        it. For JSON that must parse, use output_schema, or a strict tool plus a prompt
        instruction.
    output_schema: JSON schema that the reply has to follow.
    """

    # A misspelled option would otherwise be dropped, and the call would run without it.
    model_config = ConfigDict(extra="forbid")

    effort: EffortLevel | None = None
    force_tool: str | None = Field(default=None, min_length=1)
    output_schema: dict[str, Any] | None = None


class LLMResponse(BaseModel):
    content: list[Block]
    tool_calls: list[ToolCall]
    stop_reason: StopReason
    raw_stop_reason: str | None = None  # the provider's own string, for reports
    stop_details: StopDetails | None = None
    usage: Usage
    model: str
    latency_ms: int
    request_id: str | None = None  # the provider's id for this call, for support and traces
    # Thinking blocks are dropped before a response becomes an LLMResponse. This counts them.
    dropped_thinking_blocks: int = Field(default=0, ge=0)


class ProviderErrorKind(StrEnum):
    RATE_LIMIT = "rate_limit"  # retryable
    OVERLOADED = "overloaded"  # retryable
    SERVER = "server"  # retryable
    TIMEOUT = "timeout"  # retryable
    CONTEXT_TOO_LONG = "context_too_long"  # not retryable, triggers forced compaction
    INVALID_REQUEST = "invalid_request"  # not retryable
    AUTH = "auth"  # not retryable


_RETRYABLE = frozenset(
    {
        ProviderErrorKind.RATE_LIMIT,
        ProviderErrorKind.OVERLOADED,
        ProviderErrorKind.SERVER,
        ProviderErrorKind.TIMEOUT,
    }
)


class ProviderError(Exception):
    def __init__(
        self,
        kind: ProviderErrorKind,
        message: str,
        retry_after_s: float | None = None,
        usage: Usage | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retry_after_s = retry_after_s
        # Set when the call was billed before it failed, for example on a body that does
        # not parse, so the cost ledger can still record it.
        self.usage = usage

    def __reduce__(self) -> tuple[Any, ...]:
        # Exception pickling replays only self.args, which would drop everything but the message.
        return (type(self), (self.kind, self.message, self.retry_after_s, self.usage))

    @property
    def retryable(self) -> bool:
        return self.kind in _RETRYABLE


# ---------- task and settings ----------
class Budgets(BaseModel):
    task_usd: float = 5.0
    task_turns: int = 150
    task_wall_minutes: int = 240
    daily_repo_usd: float = 25.0


class RepoSettings(BaseModel):
    repo: str  # "owner/name" or "local:/abs/path"
    plan_approval_required: bool = False
    setup_command: str | None = None  # runs with network, before the agent phase
    test_command: str | None = None  # e.g. "pytest -q"
    budgets: Budgets = Field(default_factory=Budgets)


class TaskSpec(BaseModel):
    task_id: str  # "lh-owner-repo-42"
    repo: str
    issue_number: int | None
    title: str
    body: str
    comments: list[str] = Field(default_factory=list)
    base_branch: str = "main"
    base_sha: str | None = None
    settings: RepoSettings


# ---------- plan, verdicts, notes ----------
class PlanStep(BaseModel):
    id: str  # "s1", "s2", ...
    description: str
    files: list[str] = Field(default_factory=list)
    done_when: str
    status: Literal["todo", "doing", "done", "skipped"] = "todo"


class Plan(BaseModel):
    summary: str
    steps: list[PlanStep]
    acceptance_checks: list[str]  # commands or observable checks
    risks: list[str] = Field(default_factory=list)


class VerifierVerdict(BaseModel):
    passed: bool
    tests_run: str  # command executed
    tests_summary: str  # "41 passed, 1 failed"
    issues: list[str] = Field(default_factory=list)
    send_back_step: str | None = None


class ProgressNotes(BaseModel):
    goal: str
    plan_status: list[PlanStep]
    files_touched: list[str]
    decisions: list[str]
    failing_tests: list[str]
    open_hypotheses: list[str]
    tried_and_failed: list[str]
    covers_turns_up_to: int


class StuckReason(StrEnum):
    NO_PROGRESS = "no_progress"
    REPEATED_FAILURE = "repeated_failure"
    EDIT_CHURN = "edit_churn"
    TOOL_ERRORS = "tool_errors"
    AGENT_ASKED = "agent_asked"


class StuckSignal(BaseModel):
    reason: StuckReason
    evidence: list[str]


class Phase(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    PLANNING = "planning"
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    CODING = "coding"
    VERIFYING = "verifying"
    PAUSED = "paused"
    DELIVERING = "delivering"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OutcomeKind(StrEnum):
    PR_OPENED = "pr_opened"
    STUCK = "stuck"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"
    ERROR = "error"


class Outcome(BaseModel):
    kind: OutcomeKind
    pr_url: str | None = None
    summary: str


class TaskStatus(BaseModel):
    task_id: str
    phase: Phase
    turn_index: int
    cost_usd: float
    last_checkpoint: str | None
    plan_version: int | None
    paused_reason: str | None = None
    outcome: Outcome | None = None


# ---------- workflow carry state (Continue-As-New) ----------
class CarryState(BaseModel):
    spec: TaskSpec
    phase: Phase
    turn_index: int = 0
    transcript_ref: BlobRef | None = None  # list[Message] json
    notes: ProgressNotes | None = None
    plan: Plan | None = None
    plan_version: int = 0
    plan_approved: bool = False
    head_sha: str | None = None
    sandbox_id: str | None = None
    verify_rounds: int = 0
    cost_usd: float = 0.0
    extra_budget_usd: float = 0.0
    extra_turns: int = 0
    started_at: AwareDatetime
    hints: list[str] = Field(default_factory=list)
    stuck_count: int = 0

    @field_validator("started_at")
    @classmethod
    def _started_at_in_utc(cls, value: datetime) -> datetime:
        # Workflow code subtracts this from workflow.now(), which is aware UTC. A naive value
        # would raise TypeError there. After a JSON round trip pydantic returns its own tzinfo
        # class, so convert to the standard library UTC.
        return value.astimezone(UTC)


# ---------- protocols ----------
class LLMProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        cache_system: bool = True,
        options: CallOptions | None = None,
    ) -> LLMResponse: ...

    async def count_tokens(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        options: CallOptions | None = None,
    ) -> int: ...


class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


class Sandbox(Protocol):
    id: str
    workdir: str  # "/workspace"

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult: ...

    async def read_file(self, path: str) -> bytes: ...

    async def write_file(self, path: str, data: bytes) -> None: ...

    async def destroy(self) -> None: ...


class SandboxLimits(BaseModel):
    cpus: float = 2.0
    memory_mb: int = 4096
    pids: int = 512
    disk_note: str = "bounded by volume"


class SandboxManager(Protocol):
    async def create(
        self, task_id: str, image: str, limits: SandboxLimits, network: bool
    ) -> Sandbox: ...

    async def get(self, sandbox_id: str) -> Sandbox | None: ...

    async def set_network(self, sandbox_id: str, enabled: bool) -> None: ...


class ToolContext(BaseModel):
    task_id: str
    turn_index: int
    blob_prefix: str
    output_max_tokens: int = 4000


class Tool(Protocol):
    spec: ToolSpec
    mutates: bool

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


class BlobStore(Protocol):
    async def put(self, data: bytes, *, key: str) -> BlobRef: ...

    async def get(self, ref: BlobRef) -> bytes: ...


class CheckpointStore(Protocol):
    async def init_task(self, task_id: str, clone_url: str, base_sha: str) -> str: ...

    async def commit(self, sandbox: Sandbox, message: str) -> str | None: ...  # None if clean

    async def restore(self, sandbox: Sandbox, sha: str) -> None: ...

    async def bundle(self, task_id: str) -> BlobRef: ...


class MemoryItem(BaseModel):
    id: str
    repo: str
    kind: Literal["repo_map", "command", "convention", "pitfall", "lesson"]
    content: str
    status: Literal["candidate", "approved", "rejected"]
    source: Literal["default_branch", "merged_pr", "human", "task"]


class MemoryStore(Protocol):
    async def search(
        self, repo: str, query: str, k: int, approved_only: bool = True
    ) -> list[MemoryItem]: ...

    async def add_candidate(self, item: MemoryItem) -> str: ...

    async def promote(self, item_id: str, by: str) -> None: ...

    async def reject(self, item_id: str, by: str) -> None: ...


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class GitHubGateway(Protocol):
    async def list_labeled_issues(self, repo: str, label: str) -> list[dict[str, Any]]: ...

    async def get_issue(self, repo: str, number: int) -> dict[str, Any]: ...

    async def list_comments_since(
        self, repo: str, number: int, since: datetime
    ) -> list[dict[str, Any]]: ...

    async def post_comment(
        self, repo: str, number: int, body: str, *, idempotency_key: str
    ) -> str: ...

    async def add_label(
        self, repo: str, number: int, label: str, *, idempotency_key: str
    ) -> None: ...

    async def remove_label(
        self, repo: str, number: int, label: str, *, idempotency_key: str
    ) -> None: ...

    async def create_pr(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        *,
        draft: bool,
        idempotency_key: str,
    ) -> str: ...

    async def pr_merged(self, repo: str, pr_number: int) -> bool: ...
