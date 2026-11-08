"""The reads behind the console API: tasks, plans, the timeline, turns and repos.

Every function takes the sessions and returns schema models or rows, so the routes stay short.
They only read. The one table the API writes is `memory_items`, and that goes through the memory
store, which keeps the rules of ADR-0010.

Lists use keyset pagination. A page asks for one row more than it returns, and the extra row says
whether there is a next page. The cursor holds the sort key of the last row returned, and the next
request selects the rows after it. The sort keys are unique for a task (see `timeline`), so a
cursor never lands inside a tie.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Integer, Text, cast, func, literal, select, tuple_, union_all
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.api import paging
from longhaul.api.redaction import redact, redact_json
from longhaul.api.schemas import (
    PlanView,
    RepoView,
    TaskPage,
    TaskRecord,
    TaskSummary,
    TimelineEvent,
    TimelineItem,
    TimelinePage,
    TimelineToolCall,
    TimelineTurn,
)
from longhaul.db.models import Event, PlanRow, RepoSetting, Task, ToolCallRow, Turn

Sessions = async_sessionmaker[AsyncSession]

# The kinds of a timeline row, in the order they sort when they share a time: the model call that
# started, then the tool calls it made, then the events recorded about them.
TURN_RANK, CALL_RANK, EVENT_RANK = 0, 1, 2

__all__ = [
    "find_task",
    "find_tool_call",
    "find_turns",
    "latest_plan",
    "list_repos",
    "list_tasks",
    "task_exists",
    "timeline",
]


# ---------- tasks ----------
def _summary(task: Task) -> TaskSummary:
    return TaskSummary(
        id=task.id,
        repo=task.repo,
        issue_number=task.issue_number,
        title=task.title,
        status=task.status,
        phase=task.phase,
        outcome=None if task.outcome is None else redact(task.outcome),
        pr_url=task.pr_url,
        model_coder=task.model_coder,
        cost_usd=float(task.cost_usd),
        turns=task.turns,
        started_at=task.started_at,
        ended_at=task.ended_at,
        created_at=task.created_at,
    )


def _record(task: Task) -> TaskRecord:
    return TaskRecord(
        **_summary(task).model_dump(),
        settings=redact_json(task.settings),
        input_tokens_total=task.input_tokens_total,
        output_tokens_total=task.output_tokens_total,
        cache_read_total=task.cache_read_total,
        compactions=task.compactions,
        max_fraction=task.max_fraction,
    )


async def list_tasks(
    sessions: Sessions, *, status: str | None, repo: str | None, limit: int, cursor: str | None
) -> TaskPage:
    """The newest tasks first. A task's key is its creation time and then its id."""
    stmt = select(Task)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if repo is not None:
        stmt = stmt.where(Task.repo == repo)
    if cursor is not None:
        stamp, task_id = paging.decode_cursor(cursor, 2)
        try:
            after = datetime.fromisoformat(str(stamp))
        except ValueError:
            raise paging.CursorError("the cursor is not valid") from None
        stmt = stmt.where(
            tuple_(Task.created_at, Task.id)
            < tuple_(literal(after, DateTime(timezone=True)), literal(str(task_id), Text))
        )
    stmt = stmt.order_by(Task.created_at.desc(), Task.id.desc()).limit(limit + 1)
    async with sessions() as session:
        rows = list((await session.scalars(stmt)).all())
    page, more = rows[:limit], len(rows) > limit
    next_cursor = None
    if more:
        last = page[-1]
        next_cursor = paging.encode_cursor([last.created_at.isoformat(), last.id])
    return TaskPage(items=[_summary(task) for task in page], next_cursor=next_cursor)


async def find_task(sessions: Sessions, task_id: str) -> TaskRecord | None:
    async with sessions() as session:
        task = await session.get(Task, task_id)
    return None if task is None else _record(task)


async def task_exists(sessions: Sessions, task_id: str) -> bool:
    async with sessions() as session:
        return await session.get(Task, task_id) is not None


async def latest_plan(sessions: Sessions, task_id: str) -> PlanView | None:
    """The newest version of the plan: the one in force, or the last one that was turned down."""
    stmt = select(PlanRow).where(PlanRow.task_id == task_id).order_by(PlanRow.version.desc())
    async with sessions() as session:
        row = (await session.scalars(stmt.limit(1))).first()
    if row is None:
        return None
    return PlanView.model_validate(
        {
            "version": row.version,
            "status": row.status,
            # The planner may have copied text from the repo, so the plan is redacted like a turn.
            "plan": redact_json(row.plan),
            "decided_by": row.decided_by,
            "decided_at": row.decided_at,
            "created_at": row.created_at,
        }
    )


# ---------- the timeline ----------
def _turn_key(turn_index: Any, attempt: Any) -> Any:
    # Zero padded, so that the text sorts as the numbers do.
    return (
        func.lpad(cast(turn_index, Text), 8, "0", type_=Text)
        + ":"
        + func.lpad(cast(attempt, Text), 4, "0", type_=Text)
    )


def _event_key(event_id: Any) -> Any:
    return func.lpad(cast(event_id, Text), 12, "0", type_=Text)


async def timeline(
    sessions: Sessions, task_id: str, *, limit: int, cursor: str | None
) -> TimelinePage:
    """Events, model calls and tool calls of one task in the order they happened.

    The three tables are merged in the database, ordered by time, then by kind, then by a key
    that is unique inside the kind (the turn and attempt, the tool call id, the event id). The
    full key is in the cursor, so a page can end in the middle of a tie and the next one carries
    on from the right row. Only the keys of the page are selected first, and the rows after.
    """
    turns = select(
        Turn.started_at.label("ts"),
        literal(TURN_RANK, Integer).label("rank"),
        _turn_key(Turn.turn_index, Turn.attempt).label("key"),
    ).where(Turn.task_id == task_id)
    calls = select(
        ToolCallRow.created_at.label("ts"),
        literal(CALL_RANK, Integer).label("rank"),
        ToolCallRow.id.label("key"),
    ).where(ToolCallRow.task_id == task_id)
    events = select(
        Event.ts.label("ts"),
        literal(EVENT_RANK, Integer).label("rank"),
        _event_key(Event.id).label("key"),
    ).where(Event.task_id == task_id)
    merged = union_all(turns, calls, events).subquery()
    keys = select(merged.c.ts, merged.c.rank, merged.c.key)
    if cursor is not None:
        stamp, rank, key = paging.decode_cursor(cursor, 3)
        try:
            after = datetime.fromisoformat(str(stamp))
        except ValueError:
            raise paging.CursorError("the cursor is not valid") from None
        keys = keys.where(
            tuple_(merged.c.ts, merged.c.rank, merged.c.key)
            > tuple_(
                literal(after, DateTime(timezone=True)),
                literal(int(rank), Integer),
                literal(str(key), Text),
            )
        )
    keys = keys.order_by(merged.c.ts, merged.c.rank, merged.c.key).limit(limit + 1)
    async with sessions() as session:
        found = [(ts, int(rank), str(key)) for ts, rank, key in (await session.execute(keys)).all()]
        page, more = found[:limit], len(found) > limit
        items = await _timeline_rows(session, task_id, page)
    next_cursor = None
    if more:
        ts, rank, key = page[-1]
        next_cursor = paging.encode_cursor([ts.isoformat(), rank, key])
    return TimelinePage(items=items, next_cursor=next_cursor)


async def _timeline_rows(
    session: AsyncSession, task_id: str, page: Sequence[tuple[datetime, int, str]]
) -> list[TimelineItem]:
    def keys_of(rank: int) -> list[str]:
        return [key for _, r, key in page if r == rank]

    turn_rows = await _turns_by_key(session, task_id, keys_of(TURN_RANK))
    call_rows = await _calls_by_key(session, task_id, keys_of(CALL_RANK))
    event_rows = await _events_by_key(session, task_id, keys_of(EVENT_RANK))
    found: dict[tuple[int, str], TimelineItem] = {}
    found.update({(TURN_RANK, key): row for key, row in turn_rows.items()})
    found.update({(CALL_RANK, key): row for key, row in call_rows.items()})
    found.update({(EVENT_RANK, key): row for key, row in event_rows.items()})
    # A row deleted between the two selects is left out.
    return [found[(rank, key)] for _, rank, key in page if (rank, key) in found]


async def _turns_by_key(
    session: AsyncSession, task_id: str, keys: list[str]
) -> dict[str, TimelineTurn]:
    if not keys:
        return {}
    pairs = []
    for key in keys:
        index, attempt = key.split(":")
        pairs.append((int(index), int(attempt)))
    stmt = select(Turn).where(
        Turn.task_id == task_id, tuple_(Turn.turn_index, Turn.attempt).in_(pairs)
    )
    found = {}
    for turn in await session.scalars(stmt):
        key = f"{turn.turn_index:08d}:{turn.attempt:04d}"
        found[key] = TimelineTurn(
            ts=turn.started_at,
            turn_index=turn.turn_index,
            attempt=turn.attempt,
            role=turn.role,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            cache_read_tokens=turn.cache_read_tokens,
            cache_write_tokens=turn.cache_write_tokens,
            cost_usd=float(turn.cost_usd),
            stop_reason=turn.stop_reason,
            latency_ms=turn.latency_ms,
            ended_at=turn.ended_at,
        )
    return found


async def _calls_by_key(
    session: AsyncSession, task_id: str, keys: list[str]
) -> dict[str, TimelineToolCall]:
    if not keys:
        return {}
    stmt = select(ToolCallRow).where(ToolCallRow.task_id == task_id, ToolCallRow.id.in_(keys))
    return {
        call.id: TimelineToolCall(
            ts=call.created_at,
            id=call.id,
            turn_index=call.turn_index,
            name=call.name,
            ok=call.ok,
            exit_code=call.exit_code,
            duration_ms=call.duration_ms,
            has_output=call.output_ref is not None,
        )
        for call in await session.scalars(stmt)
    }


async def _events_by_key(
    session: AsyncSession, task_id: str, keys: list[str]
) -> dict[str, TimelineEvent]:
    if not keys:
        return {}
    stmt = select(Event).where(Event.task_id == task_id, Event.id.in_([int(k) for k in keys]))
    return {
        f"{event.id:012d}": TimelineEvent(
            ts=event.ts, id=event.id, type=event.type, payload=redact_json(event.payload)
        )
        for event in await session.scalars(stmt)
    }


# ---------- turns and tool calls ----------
async def find_turns(sessions: Sessions, task_id: str, turn_index: int) -> list[Turn]:
    """Every attempt of one turn, in attempt order. Empty when the task has no such turn."""
    stmt = (
        select(Turn)
        .where(Turn.task_id == task_id, Turn.turn_index == turn_index)
        .order_by(Turn.attempt)
    )
    async with sessions() as session:
        return list((await session.scalars(stmt)).all())


async def find_tool_call(sessions: Sessions, task_id: str, call_id: str) -> ToolCallRow | None:
    """A tool call of this task. A call id that belongs to another task is not found."""
    stmt = select(ToolCallRow).where(ToolCallRow.task_id == task_id, ToolCallRow.id == call_id)
    async with sessions() as session:
        return (await session.scalars(stmt)).first()


# ---------- repos ----------
async def list_repos(sessions: Sessions) -> list[RepoView]:
    counts = select(Task.repo, func.count().label("n")).group_by(Task.repo).subquery()
    stmt = (
        select(RepoSetting, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.repo == RepoSetting.repo)
        .order_by(RepoSetting.repo)
    )
    async with sessions() as session:
        rows = (await session.execute(stmt)).all()
    return [
        RepoView(
            repo=row.repo,
            allowlisted=row.allowlisted,
            settings=redact_json(row.settings),
            tasks=int(tasks),
            created_at=row.created_at,
        )
        for row, tasks in rows
    ]
