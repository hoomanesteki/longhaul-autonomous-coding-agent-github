"""SQLAlchemy models. Each story that adds a table adds one migration."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class RepoSetting(Base):
    __tablename__ = "repo_settings"

    repo: Mapped[str] = mapped_column(Text, primary_key=True)
    allowlisted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_repo", "repo"), Index("ix_tasks_status", "status"))

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    issue_number: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    phase: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(Text)
    model_coder: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    turns: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # The context totals of the task (LH-406). Activities write them and the workflow never does.
    # The three token totals follow the cost ledger rows of the task, the model calls of the
    # compactor included. They do not overlap: `input_tokens_total` is what the model read that
    # was not in the prompt cache (input and cache writes), `cache_read_total` what it read from
    # the cache, so their sum is every prompt token. `compactions` counts the compaction events.
    # `max_fraction` is the largest share of the context window that one request took, as llm_turn
    # projected it before sending. 64 bit, because a task re-sends its prompt on every turn.
    input_tokens_total: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    output_tokens_total: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    cache_read_total: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    compactions: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_fraction: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    # The newest issue comment the poller has handled (LH-308): its time, for the `since` of the
    # next request, and its id, which tells it from a comment of the same second. Null until the
    # poller has picked the task up.
    last_comment_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_comment_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Checkpoint(Base):
    """One saved workspace state of a task: a commit in its bare repo (ADR-0006)."""

    __tablename__ = "checkpoints"
    __table_args__ = (
        CheckConstraint("kind IN ('tool', 'step', 'compaction', 'bundle')", name="kind"),
        Index("ix_checkpoints_task_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    sha: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    step_id: Mapped[str | None] = mapped_column(Text)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # A BlobRef (key, sha256, size) as JSON, so a reader can verify the bundle it downloads.
    bundle_ref: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Turn(Base):
    """One model call. The activity writes a row for every call that was billed.

    `attempt` counts the calls of one turn: a turn is asked again after a reply that was cut
    off at max_tokens, after a pause_turn, and after a retry. A call that failed before the
    provider billed it leaves no row. A billed call that failed leaves one with its usage and
    no reply. The cost ledger has a row of its own for each of these calls.
    """

    __tablename__ = "turns"

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    turn_index: Mapped[int] = mapped_column(Integer, primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    # Blob keys. The request is the transcript that was sent, the response the provider's reply.
    request_ref: Mapped[str | None] = mapped_column(Text)
    response_ref: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, server_default=text("0")
    )
    stop_reason: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # cache_read / (input + cache_read), from the counts above (LH-405). It is kept as a column
    # so a report or a dashboard can average it by task without redoing the sum for every row.
    cache_ratio: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))


class ToolCallRow(Base):
    """One tool call that ran. The key is the provider's tool_use id.

    A retried activity writes the row again, with its own attempt number, so the row always
    describes the attempt that finished last.
    """

    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_calls_task_turn", "task_id", "turn_index"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    args_ref: Mapped[str | None] = mapped_column(Text)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    output_ref: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pre_sha: Mapped[str | None] = mapped_column(Text)
    post_sha: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlanRow(Base):
    """One version of the plan of a task (LH-501).

    The planner's plan is version 1. A plan that a person rejects is followed by a new version
    (LH-502), and the old row is marked rejected, so the table is the history of the plan and
    the workflow's CarryState holds only the version in force. `plan` is the contract (`Plan`)
    as JSON. `decided_by` and `decided_at` stay empty until someone approves or rejects it, and
    a plan that nobody answers stays proposed. `superseded` is for a later story that replaces a
    plan without a decision.
    """

    __tablename__ = "plans"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')", name="status"
        ),
    )

    task_id: Mapped[str] = mapped_column(Text, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'proposed'"))
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Event(Base):
    """One thing that happened to a task, such as a sandbox that had to be recreated (LH-206).

    A log, not a ledger: the same event can be recorded twice when an activity ran twice and did
    the thing twice. `ts` is when it happened and `created_at` when the row was written, which
    differ if a writer ever sets `ts` itself. `type` is free text. Section 3.5 lists the first
    types, and later stories add more (memory_promoted and memory_rejected in LH-606), so a CHECK
    on it would turn each new type into a migration.
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_task_id", "task_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CostLedgerRow(Base):
    """One model call and what it cost (LH-701), added up by task and by repo and day.

    The ledger is append only. Every call that was billed is a row, so a retried call is another
    row, with the Temporal attempt that made it. One turn can have several rows with the same
    attempt: a reply cut off at max_tokens is asked again, and a paused reply is continued, each
    by a new run of the activity that starts at attempt 1. Nothing but `id` can be the key.

    `ts` is the time of the call, and `created_at` the time of the insert, as on every table.
    """

    __tablename__ = "cost_ledger"
    __table_args__ = (
        Index("ix_cost_ledger_task_id", "task_id"),
        # The daily limit of a repo sums the rows since midnight UTC.
        Index("ix_cost_ledger_repo_ts", "repo", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    cache_write_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SideEffect(Base):
    """One GitHub write that happened, found again by its idempotency key (LH-304).

    The ledger of github/side_effects.py. A row is written after the effect, so a retry that
    finds the key returns `external_id` and writes nothing. `payload_hash` lets the next use
    of a key tell a retry (same payload) from a different effect that reused the key.
    """

    __tablename__ = "side_effects"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('comment', 'label_add', 'label_remove', 'pr', 'push')", name="kind"
        ),
        Index("ix_side_effects_task_id", "task_id"),
    )

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    task_id: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    # A comment id, a pull request URL, a label name or a pushed commit id, by kind.
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MemoryItemRow(Base):
    """One thing the agent remembers about a repo (ADR-0010).

    The row is named for its table because longhaul.contracts already has a MemoryItem, the
    shape that crosses module boundaries. Only an `approved` row may reach a prompt (invariant
    I7), and a new row is a `candidate` unless its writer says otherwise, so an insert that
    forgets about trust cannot put an item in front of the model.
    """

    __tablename__ = "memory_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('repo_map', 'command', 'convention', 'pitfall', 'lesson')", name="kind"
        ),
        CheckConstraint("status IN ('candidate', 'approved', 'rejected')", name="status"),
        CheckConstraint(
            "source IN ('default_branch', 'merged_pr', 'human', 'task')", name="source"
        ),
        # Nearest neighbour by cosine distance. m and ef_construction are pgvector's own
        # defaults, written out so that a change of the default does not change this index.
        Index(
            "ix_memory_items_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # The same note is stored once per repo and kind (LH-603). Rows without a hash are
        # not compared, because NULLs differ from each other in a unique index.
        Index(
            "uq_memory_items_repo_kind_content_hash",
            "repo",
            "kind",
            "content_hash",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Every row has a vector, so every item can be found by meaning. The width is part of the
    # type: a vector of another width is refused on insert. It is the EMBEDDING_DIM of
    # longhaul.memory.embedder, written out because this module imports nothing from the memory
    # package, which keeps an import cycle out of reach. A unit test keeps the two equal.
    embedding: Mapped[list[float]] = mapped_column(Vector(384), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'candidate'"))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # What the source points at: a commit sha, a pull request number or a task id.
    source_ref: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(Text)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # sha256 of the content with its case and spacing normalized, as hex. The store sets it
    # (longhaul.memory.store.content_hash). A row that something else wrote has none.
    content_hash: Mapped[str | None] = mapped_column(Text)
