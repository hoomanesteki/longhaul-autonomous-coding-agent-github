"""The rows that a run report is made from (LH-705).

A report says how the agent got to where it is, so it is read from what the activities wrote
while the task ran: the `turns`, `tool_calls`, `events`, `plans` and `cost_ledger` tables and,
when there is one, the row in `tasks`. It is never read from workflow state. Workflow code cannot
touch the database, and a report has to be possible for a task whose workflow is gone.

Two tasks have no `tasks` row, a task started with `longhaul run` and one the poller has not
written yet. Their report is built from the other tables, and `ReportData.task` is None. Only a
task with no rows at all is unknown (`read_report_data` raises LookupError).

The cost, the tokens and the cache ratio come from the cost ledger and not from the `turns`
table, because the ledger is where every model call is priced, the compactor's included
(invariant I6). `turns` holds the calls of the planner, the coder and the verifier, which is what
the report counts as turns.

These classes are plain data so that the renderer can be tested with rows made in memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.contracts import Plan, Usage
from longhaul.db.models import CostLedgerRow, Event, PlanRow, Task, ToolCallRow, Turn


@dataclass(frozen=True)
class TaskInfo:
    """The `tasks` row, for a task that has one."""

    repo: str
    issue_number: int | None
    title: str
    outcome: str | None = None
    pr_url: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None


@dataclass(frozen=True)
class TurnInfo:
    """One model turn. A turn that was asked again has one row for each call, with one index."""

    role: str
    turn_index: int
    started_at: datetime
    ended_at: datetime | None = None


@dataclass(frozen=True)
class ToolTotals:
    """What one tool did over the task: calls, the ones that failed, and the time they took."""

    name: str
    calls: int
    errors: int
    total_ms: int


@dataclass(frozen=True)
class EventInfo:
    ts: datetime
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class PlanVersion:
    """One version of the plan, as the `plans` table has it. The steps are as they were stored."""

    version: int
    plan: Plan
    status: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None


@dataclass(frozen=True)
class Spend:
    """What the model calls of the task cost, added up over the cost ledger."""

    calls: int = 0
    usd: Decimal = Decimal(0)
    usage: Usage = field(default_factory=Usage)
    repo: str | None = None


@dataclass(frozen=True)
class ReportData:
    task_id: str
    task: TaskInfo | None = None
    turns: list[TurnInfo] = field(default_factory=list)
    tools: list[ToolTotals] = field(default_factory=list)
    events: list[EventInfo] = field(default_factory=list)
    plans: list[PlanVersion] = field(default_factory=list)
    spend: Spend = field(default_factory=Spend)

    @property
    def known(self) -> bool:
        """Whether the database has anything at all about the task."""
        return bool(self.task or self.turns or self.tools or self.events or self.plans) or (
            self.spend.calls > 0
        )


async def read_report_data(sessions: async_sessionmaker[AsyncSession], task_id: str) -> ReportData:
    """Every row of one task that the report uses. Raises LookupError for a task with none."""
    async with sessions() as session:
        data = ReportData(
            task_id=task_id,
            task=await _task(session, task_id),
            turns=await _turns(session, task_id),
            tools=await _tools(session, task_id),
            events=await _events(session, task_id),
            plans=await _plans(session, task_id),
            spend=await _spend(session, task_id),
        )
    if not data.known:
        raise LookupError(f"no task {task_id} in the database")
    return data


async def _task(session: AsyncSession, task_id: str) -> TaskInfo | None:
    row = (await session.execute(select(Task).where(Task.id == task_id))).scalars().first()
    if row is None:
        return None
    return TaskInfo(
        repo=row.repo,
        issue_number=row.issue_number,
        title=row.title,
        outcome=row.outcome,
        pr_url=row.pr_url,
        started_at=row.started_at,
        ended_at=row.ended_at,
    )


async def _turns(session: AsyncSession, task_id: str) -> list[TurnInfo]:
    stmt = (
        select(Turn.role, Turn.turn_index, Turn.started_at, Turn.ended_at)
        .where(Turn.task_id == task_id)
        .order_by(Turn.started_at, Turn.turn_index, Turn.attempt)
    )
    return [TurnInfo(*row) for row in (await session.execute(stmt)).all()]


async def _tools(session: AsyncSession, task_id: str) -> list[ToolTotals]:
    failed = func.sum(case((ToolCallRow.ok.is_(False), 1), else_=0))
    stmt = (
        select(
            ToolCallRow.name,
            func.count(),
            func.coalesce(failed, 0),
            func.coalesce(func.sum(ToolCallRow.duration_ms), 0),
        )
        .where(ToolCallRow.task_id == task_id)
        .group_by(ToolCallRow.name)
        .order_by(ToolCallRow.name)
    )
    return [ToolTotals(*row) for row in (await session.execute(stmt)).all()]


async def _events(session: AsyncSession, task_id: str) -> list[EventInfo]:
    stmt = select(Event.ts, Event.type, Event.payload).where(Event.task_id == task_id)
    # The id breaks a tie between two events of the same instant in the order they were written.
    rows = (await session.execute(stmt.order_by(Event.ts, Event.id))).all()
    return [EventInfo(ts, type, dict(payload)) for ts, type, payload in rows]


async def _plans(session: AsyncSession, task_id: str) -> list[PlanVersion]:
    stmt = select(PlanRow).where(PlanRow.task_id == task_id).order_by(PlanRow.version)
    return [
        PlanVersion(
            version=row.version,
            plan=Plan.model_validate(row.plan),
            status=row.status,
            created_at=row.created_at,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
        )
        for row in (await session.execute(stmt)).scalars()
    ]


async def _spend(session: AsyncSession, task_id: str) -> Spend:
    stmt = select(
        func.count(),
        func.coalesce(func.sum(CostLedgerRow.usd), 0),
        func.coalesce(func.sum(CostLedgerRow.input_tokens), 0),
        func.coalesce(func.sum(CostLedgerRow.output_tokens), 0),
        func.coalesce(func.sum(CostLedgerRow.cache_read_tokens), 0),
        func.coalesce(func.sum(CostLedgerRow.cache_write_tokens), 0),
        func.max(CostLedgerRow.repo),
    ).where(CostLedgerRow.task_id == task_id)
    calls, usd, fresh, out, read, wrote, repo = (await session.execute(stmt)).one()
    usage = Usage(
        input_tokens=int(fresh),
        output_tokens=int(out),
        cache_read_tokens=int(read),
        cache_write_tokens=int(wrote),
    )
    return Spend(calls=int(calls), usd=Decimal(usd), usage=usage, repo=repo)
