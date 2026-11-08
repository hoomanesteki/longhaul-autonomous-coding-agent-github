"""The rows that activities write: one per model call, tool call, checkpoint, event and plan.

`Recorder` is the small interface the activities use, so a unit test can run them without
Postgres. `SqlRecorder` writes the `turns`, `tool_calls`, `checkpoints`, `events` and `plans`
tables and `MemoryRecorder` keeps the same rows in memory. Both also read back the last call of a
tool (`last_tool_call`), which `deliver` uses to say how a task was tested (LH-306), and the step
checkpoints of a task (`step_checkpoints`), which `build_pr_branch` turns into one commit for each
step (LH-503). Neither is in the `Recorder` protocol: only the delivery activities read rows, and
they name what they need.
tests/integration/test_records.py runs the cases of tests/unit/test_records.py against both.

Temporal runs an activity again after a crash or a timeout, and the second run writes what the
first run may already have written. A turn, a tool call and a checkpoint are therefore written
once, however often they are recorded: the first two as upserts on their key, the checkpoint by
looking for the same sha, kind and step first, because its table has no natural key. The step is
part of it because a step that is done with no new work has the sha of the step before it
(LH-503), and each of the two still has its own row. An event is a log line.
The same event twice means that the thing happened twice, so it is written every time. A plan is
an upsert on its task and version, and it only ever rewrites the plan itself: the status and the
decision of a row that someone has already decided on are left as they are. The decision itself
(`record_plan_decision`, LH-502) is written once: it only changes a row that is still proposed,
so the first answer stands and its time does not move when the activity runs again.

Two of the totals in `tasks` come from here (LH-406). `record_request_fraction` keeps the largest
share of the window that a request took, as a maximum, so it cannot count a request twice. A
`compaction` event sets `tasks.compactions` to the number of different compaction events of the
task, counted from the log and not added to a counter. An activity that ran again after its write
went through leaves a second, identical line in the log (an event is a log line), and the count
stays right. Two real compactions differ in the tokens they started from, so they are two. A
task with no row in `tasks`, as in a local run, is not an error: the update changes nothing, as
it does for the cost ledger (cost/ledger.py).

`ended_before` tells the janitor (LH-507) which tasks ended before a time. A task has ended when
it has an `outcome` event, and the time of the end is the time of that event. It is not in the
`Recorder` protocol either: only the janitor asks.

`events_of` reads a task's events of some types back, oldest first. The lessons of a finished task
(LH-605) are drafted from its stuck events and the verifier's issues, which the workflow wrote to
the log and no longer holds. It is outside the `Recorder` protocol too: only that activity reads.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import distinct, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.context.tokens import cache_ratio
from longhaul.contracts import BlobRef, Plan, Usage
from longhaul.db.models import Checkpoint, Event, PlanRow, Task, ToolCallRow, Turn

# The type of the event that the workflow records for a compaction (durable/workflows/task.py).
# The workflow cannot import this module, so each side has the name, and a unit test keeps the two
# equal.
COMPACTION_EVENT = "compaction"
# The status of a plan that nobody has decided on yet (plan section 3.5).
PROPOSED = "proposed"

# The type of the event that the workflow records when a task ends (durable/acts.OUTCOME_EVENT,
# LH-704). It dates the end of a task, which `ended_before` reads for the janitor (LH-507). A unit
# test keeps the two names equal.
OUTCOME_EVENT = "outcome"

# The kind of the checkpoint that a step leaves when it is done (plan section 3.5).
STEP_KIND = "step"


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
    commit, and record_step writes `step` for a step that is done (LH-503), with its `step_id`.
    `bundle_ref` is set for a bundle only.
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


@dataclass(frozen=True)
class PlanRecord:
    """One version of the plan of a task, as store_plan writes it (LH-501).

    A plan starts as `proposed`. A person approves or rejects it at the plan gate (LH-502), and
    the row then holds the status and the login of whoever decided.
    """

    task_id: str
    version: int
    plan: Plan
    status: str = PROPOSED
    decided_by: str | None = None


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

    async def record_request_fraction(self, task_id: str, fraction: float) -> None:
        """Note that a request took `fraction` of the window. The task keeps the largest.

        This is `tasks.max_fraction`. It only goes up, so recording a request again, or a smaller
        one, changes nothing.
        """
        ...

    async def record_plan(self, record: PlanRecord) -> None:
        """Save a version of the plan. Saving the same version again rewrites only its plan."""
        ...

    async def record_plan_decision(
        self, task_id: str, version: int, *, approved: bool, decided_by: str
    ) -> None:
        """Mark a proposed version `approved` or `rejected`, with who decided and when.

        A version that is already decided, or was never stored, is left as it is.
        """
        ...


class MemoryRecorder:
    """The rows in memory, for tests. Public, so that a test can read what was written."""

    def __init__(self) -> None:
        self.turns: list[TurnRecord] = []
        self.tool_calls: dict[str, ToolCallRecord] = {}
        self.checkpoints: list[CheckpointRecord] = []
        self.events: list[EventRecord] = []
        self.max_fractions: dict[str, float] = {}
        self.plans: dict[tuple[str, int], PlanRecord] = {}
        # When each task ended, from its outcome event. A test may set a time directly.
        self.ended: dict[str, datetime] = {}

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
        if type == OUTCOME_EVENT:
            self.ended.setdefault(task_id, datetime.now(UTC))

    async def ended_before(self, task_ids: Sequence[str], cutoff: datetime) -> set[str]:
        return {t for t in task_ids if t in self.ended and self.ended[t] < cutoff}

    async def events_of(self, task_id: str, types: Sequence[str]) -> list[EventRecord]:
        return [e for e in self.events if e.task_id == task_id and e.type in types]

    async def record_plan(self, record: PlanRecord) -> None:
        key = (record.task_id, record.version)
        kept = self.plans.get(key)
        # A row that has a status already keeps it, as the SQL upsert does.
        self.plans[key] = record if kept is None else replace(kept, plan=record.plan)

    async def step_checkpoints(self, task_id: str) -> list[CheckpointRecord]:
        return [c for c in self.checkpoints if c.task_id == task_id and c.kind == STEP_KIND]

    async def record_plan_decision(
        self, task_id: str, version: int, *, approved: bool, decided_by: str
    ) -> None:
        kept = self.plans.get((task_id, version))
        if kept is None or kept.status != PROPOSED:
            return
        status = "approved" if approved else "rejected"
        self.plans[(task_id, version)] = replace(kept, status=status, decided_by=decided_by)

    async def last_tool_call(self, task_id: str, name: str) -> ToolCallRecord | None:
        found = [c for c in self.tool_calls.values() if (c.task_id, c.name) == (task_id, name)]
        # The position breaks a tie between two calls of one turn in favour of the one recorded
        # last, as created_at does in the table.
        latest = max(enumerate(found), key=lambda item: (item[1].turn_index, item[0]), default=None)
        return None if latest is None else latest[1]

    async def record_request_fraction(self, task_id: str, fraction: float) -> None:
        self.max_fractions[task_id] = max(fraction, self.max_fractions.get(task_id, 0.0))


def _same_checkpoint(a: CheckpointRecord, b: CheckpointRecord) -> bool:
    return (a.task_id, a.sha, a.kind, a.step_id) == (b.task_id, b.sha, b.kind, b.step_id)


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
            # IS NOT DISTINCT FROM: a tool checkpoint has no step, and NULL = NULL is not true.
            Checkpoint.step_id.is_not_distinct_from(record.step_id),
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
            if type == COMPACTION_EVENT:
                # The new line has to be visible to the count, and a count is not an increment:
                # see the module docstring.
                await session.flush()
                compactions = (
                    select(func.count(distinct(Event.payload)))
                    .where(Event.task_id == task_id, Event.type == COMPACTION_EVENT)
                    .scalar_subquery()
                )
                await session.execute(
                    update(Task).where(Task.id == task_id).values(compactions=compactions)
                )

    async def ended_before(self, task_ids: Sequence[str], cutoff: datetime) -> set[str]:
        if not task_ids:
            return set()
        async with self._sessions() as session:
            rows = await session.execute(
                select(Event.task_id)
                .where(Event.task_id.in_(task_ids), Event.type == OUTCOME_EVENT, Event.ts < cutoff)
                .distinct()
            )
            return {row[0] for row in rows}

    async def events_of(self, task_id: str, types: Sequence[str]) -> list[EventRecord]:
        if not types:
            return []
        async with self._sessions() as session:
            rows = await session.execute(
                select(Event.task_id, Event.type, Event.payload)
                .where(Event.task_id == task_id, Event.type.in_(types))
                # The id breaks a tie between two events written in the same instant.
                .order_by(Event.ts, Event.id)
            )
            return [EventRecord(row[0], row[1], dict(row[2])) for row in rows]

    async def record_request_fraction(self, task_id: str, fraction: float) -> None:
        # One statement, so two requests that finish together cannot lower the maximum.
        stmt = (
            update(Task)
            .where(Task.id == task_id)
            .values(max_fraction=func.greatest(Task.max_fraction, fraction))
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    async def record_plan(self, record: PlanRecord) -> None:
        values = {
            "task_id": record.task_id,
            "version": record.version,
            "plan": record.plan.model_dump(mode="json"),
            "status": record.status,
        }
        stmt = insert(PlanRow).values(values)
        # A retry writes the same plan again. The status and the decision are not touched, so a
        # plan that has been approved in the meantime stays approved.
        stmt = stmt.on_conflict_do_update(
            index_elements=["task_id", "version"], set_={"plan": stmt.excluded.plan}
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    async def step_checkpoints(self, task_id: str) -> list[CheckpointRecord]:
        """The checkpoints of the done steps of a task, in the order they were saved."""
        stmt = (
            select(Checkpoint)
            .where(Checkpoint.task_id == task_id, Checkpoint.kind == STEP_KIND)
            .order_by(Checkpoint.id)
        )
        async with self._sessions() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [
            CheckpointRecord(
                task_id=row.task_id,
                sha=row.sha,
                kind=row.kind,
                turn_index=row.turn_index,
                step_id=row.step_id,
            )
            for row in rows
        ]

    async def record_plan_decision(
        self, task_id: str, version: int, *, approved: bool, decided_by: str
    ) -> None:
        # Only a proposed row changes, in one statement, so a rerun finds the answer already
        # there and leaves the author and the time of the first one.
        stmt = (
            update(PlanRow)
            .where(
                PlanRow.task_id == task_id, PlanRow.version == version, PlanRow.status == PROPOSED
            )
            .values(
                status="approved" if approved else "rejected",
                decided_by=decided_by,
                decided_at=func.now(),
            )
        )
        async with self._sessions.begin() as session:
            await session.execute(stmt)

    async def last_tool_call(self, task_id: str, name: str) -> ToolCallRecord | None:
        """The call of this tool in the latest turn, or None if the task never used it.

        A turn can hold several calls of one tool. The one written last wins, and `created_at`
        says which that is. A retried call keeps the `created_at` of its first attempt, which
        can only matter between two calls of one turn.
        """
        stmt = (
            select(ToolCallRow)
            .where(ToolCallRow.task_id == task_id, ToolCallRow.name == name)
            .order_by(
                ToolCallRow.turn_index.desc(), ToolCallRow.created_at.desc(), ToolCallRow.id.desc()
            )
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.execute(stmt)).scalars().first()
        return None if row is None else _tool_call_record(row)


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


def _tool_call_record(row: ToolCallRow) -> ToolCallRecord:
    return ToolCallRecord(
        id=row.id,
        task_id=row.task_id,
        turn_index=row.turn_index,
        name=row.name,
        ok=row.ok,
        attempt=row.attempt,
        args_ref=row.args_ref,
        exit_code=row.exit_code,
        output_ref=row.output_ref,
        duration_ms=row.duration_ms,
        pre_sha=row.pre_sha,
        post_sha=row.post_sha,
    )


def _checkpoint_values(r: CheckpointRecord) -> dict[str, Any]:
    return {
        "task_id": r.task_id,
        "sha": r.sha,
        "kind": r.kind,
        "step_id": r.step_id,
        "turn_index": r.turn_index,
        "bundle_ref": r.bundle_ref.model_dump(mode="json") if r.bundle_ref is not None else None,
    }
