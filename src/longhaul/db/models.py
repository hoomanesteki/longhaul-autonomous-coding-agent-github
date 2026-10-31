"""SQLAlchemy models. Each story that adds a table adds one migration."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
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
    no reply, so the cost ledger of Phase 7 can still account for it.
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
