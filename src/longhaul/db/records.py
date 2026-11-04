"""The rows that activities write: one per model call, tool call, checkpoint and event.

`Recorder` is the small interface the activities use, so a unit test can run them without
Postgres. `SqlRecorder` writes the `turns`, `tool_calls`, `checkpoints` and `events` tables and
`MemoryRecorder` keeps the same rows in memory. tests/integration/test_records.py runs the cases
of tests/unit/test_records.py against both.

Temporal runs an activity again after a crash or a timeout, and the second run writes what the
first run may already have written. A turn, a tool call and a checkpoint are therefore written
once, however often they are recorded: the first two as upserts on their key, the checkpoint by
looking for the same sha first, because its table has no natural key. An event is a log line.
The same event twice means that the thing happened twice, so it is written every time.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.context.tokens import cache_ratio
from longhaul.contracts import BlobRef, Usage
from longhaul.db.models import Checkpoint, Event, ToolCallRow, Turn


@dataclass(frozen=True)
class TurnRecord:
    """One model call. `usage` is all there is to record for a call that failed after billing."""

    task_id: str
    turn_index: int
    attempt: int
    role: str
    started_at: datetime
    request_ref: str | None = None  # blob key of the transcript that was sent
    response_ref: str | None = None  # blob key of the reply; None when the call failed
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0  # the price of the call, as the cost ledger has it
    stop_reason: str | None = None
    latency_ms: int | None = None
    ended_at: datetime | None = None

    @property
    def cache_ratio(self) -> float:
        """cache_read / (input + cache_read) for the call (LH-405).

        Worked out from `usage`, not stored beside it, so the ratio cannot disagree with the
        counts it comes from.
        """
        return cache_ratio(self.usage)


@dataclass(frozen=True)
class ToolCallRecord:
    """One tool call that ran. `id` is the provider's tool_use id."""

    id: str
    task_id: str
    turn_index: int
    name: str
    ok: bool
    attempt: int = 1  # the activity attempt that finished the call
    args_ref: str | None = None
    exit_code: int | None = None
    output_ref: str | None = None
    duration_ms: int = 0
    pre_sha: str | None = None
    post_sha: str | None = None


@dataclass(frozen=True)
class CheckpointRecord:
    """One saved workspace state: a commit in the task's bare repo (ADR-0006).

    `kind` is one of tool, step, compaction and bundle. tool_exec writes `tool` after each
    commit. `bundle_ref` is set for a bundle only.
    """

    task_id: str
    sha: str
    kind: str
    turn_index: int
    step_id: str | None = None
    bundle_ref: BlobRef | None = None


@dataclass(frozen=True)
class EventRecord:
    """One thing that happened to a task. `type` is one of the names in plan section 3.5."""

    task_id: str
    type: str
    payload: dict[str, Any]


class Recorder(Protocol):
    async def next_attempt(self, task_id: str, turn_index: int) -> int:
        """The attempt number for the next model call of a turn: one more than the rows so far.

        A call that left no row, because it failed before it was billed, does not use one up.
        """
        ...

    async def record_turn(self, record: TurnRecord) -> None: ...

    async def record_tool_call(self, record: ToolCallRecord) -> None: ...

    async def record_checkpoint(self, record: CheckpointRecord) -> None:
        """Save the checkpoint, unless this task already has one with the same sha and kind."""
        ...

    async def record_event(self, task_id: str, type: str, payload: Mapping[str, Any]) -> None:
        """Add an event to the log of the task. The payload must be JSON."""
        ...


class MemoryRecorder:
    """The rows in memory, for tests. Public, so that a test can read what was written."""

    def __init__(self) -> None:
        self.turns: list[TurnRecord] = []
        self.tool_calls: dict[str, ToolCallRecord] = {}
        self.checkpoints: list[CheckpointRecord] = []
        self.events: list[EventRecord] = []

    async def next_attempt(self, task_id: str, turn_index: int) -> int:
        taken = [
            t.attempt for t in self.turns if t.task_id == task_id and t.turn_index == turn_index
        ]
        return max(taken, default=0) + 1

    async def record_turn(self, record: TurnRecord) -> None:
        key = (record.task_id, record.turn_index, record.attempt)
        kept = [t for t in self.turns if (t.task_id, t.turn_index, t.attempt) != key]
        self.turns = [*kept, record]

    async def record_tool_call(self, record: ToolCallRecord) -> None:
        self.tool_calls[record.id] = record

    async def record_checkpoint(self, record: CheckpointRecord) -> None:
        if not any(_same_checkpoint(c, record) for c in self.checkpoints):
            self.checkpoints.append(record)

    async def record_event(self, task_id: str, type: str, payload: Mapping[str, Any]) -> None:
        self.events.append(EventRecord(task_id, type, dict(payload)))


def _same_checkpoint(a: CheckpointRecord, b: CheckpointRecord) -> bool:
    return (a.task_id, a.sha, a.kind) == (b.task_id, b.sha, b.kind)


_TURN_KEY = ("task_id", "turn_index", "attempt")


class SqlRecorder:
    """Writes to Postgres through an async session factory."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def next_attempt(self, task_id: str, turn_index: int) -> int:
        stmt = select(func.coalesce(func.max(Turn.attempt), 0) + 1).where(
            Turn.task_id == task_id, Turn.turn_index == turn_index
        )
        async with self._sessions() as session:
            return int((await session.execute(stmt)).scalar_one())

    async def record_turn(self, record: TurnRecord) -> None:
        values = _turn_values(record)
        stmt = insert(Turn).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=list(_TURN_KEY),
            set_={name: stmt.excluded[name] for name in values if name not in _TURN_KEY},
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    async def record_tool_call(self, record: ToolCallRecord) -> None:
        values = _tool_call_values(record)
        stmt = insert(ToolCallRow).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={name: stmt.excluded[name] for name in values if name != "id"},
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    async def record_checkpoint(self, record: CheckpointRecord) -> None:
        seen = select(Checkpoint.id).where(
            Checkpoint.task_id == record.task_id,
            Checkpoint.sha == record.sha,
            Checkpoint.kind == record.kind,
        )
        async with self._sessions.begin() as session:
            # No unique key to upsert on, so look first. Two attempts of one activity do not
            # run at the same moment unless a worker hung past its timeout, and the cost of that
            # race is one extra row.
            if (await session.execute(seen.limit(1))).first() is None:
                await session.execute(insert(Checkpoint).values(_checkpoint_values(record)))

    async def record_event(self, task_id: str, type: str, payload: Mapping[str, Any]) -> None:
        async with self._sessions.begin() as session:
            session.add(Event(task_id=task_id, type=type, payload=dict(payload)))


def _turn_values(r: TurnRecord) -> dict[str, Any]:
    return {
        "task_id": r.task_id,
        "turn_index": r.turn_index,
        "attempt": r.attempt,
        "role": r.role,
        "request_ref": r.request_ref,
        "response_ref": r.response_ref,
        "input_tokens": r.usage.input_tokens,
        "output_tokens": r.usage.output_tokens,
        "cache_read_tokens": r.usage.cache_read_tokens,
        "cache_write_tokens": r.usage.cache_write_tokens,
        "cache_ratio": r.cache_ratio,
        # str() first: Decimal(0.1) would carry the binary rounding error of the float.
        "cost_usd": Decimal(str(r.cost_usd)),
        "stop_reason": r.stop_reason,
        "latency_ms": r.latency_ms,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
    }


def _tool_call_values(r: ToolCallRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "task_id": r.task_id,
        "turn_index": r.turn_index,
        "name": r.name,
        "args_ref": r.args_ref,
        "ok": r.ok,
        "exit_code": r.exit_code,
        "output_ref": r.output_ref,
        "duration_ms": r.duration_ms,
        "pre_sha": r.pre_sha,
        "post_sha": r.post_sha,
        "attempt": r.attempt,
    }


def _checkpoint_values(r: CheckpointRecord) -> dict[str, Any]:
    return {
        "task_id": r.task_id,
        "sha": r.sha,
        "kind": r.kind,
        "step_id": r.step_id,
        "turn_index": r.turn_index,
        "bundle_ref": r.bundle_ref.model_dump(mode="json") if r.bundle_ref is not None else None,
    }
