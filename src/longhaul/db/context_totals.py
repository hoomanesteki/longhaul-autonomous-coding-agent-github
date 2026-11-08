"""What `longhaul status` shows of the context of a task (LH-407).

A task that the poller started has its totals in its `tasks` row (LH-406): the activities keep
`compactions` and `max_fraction` there. A task that `longhaul run` started has no row, so those
columns do not exist for it and the log says the same thing in other words. The compactions are
the distinct `compaction` events, which is also how the column is counted. The largest request is
the largest prompt that the provider reported for a model turn of the coder, as a share of the
window of the run. That is the figure the long-task test checks. It differs a little from the
column, which holds the estimate that `llm_turn` made before it sent the request.

A task that left no turn and has no row is not known to the database, and the answer is None.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.db.models import Event, Task, Turn
from longhaul.db.records import COMPACTION_EVENT

# The role whose requests carry the task. The compactor's own calls are small, and they are
# not what a window limit is about.
CODER = "coder"


@dataclass(frozen=True)
class ContextTotals:
    """The compactions of a task and the largest share of the window one request took.

    `max_fraction` is None when the task has no row and the window of the run is not known.
    """

    compactions: int
    max_fraction: float | None


async def read_context_totals(
    sessions: async_sessionmaker[AsyncSession], task_id: str, window: int | None
) -> ContextTotals | None:
    """The totals of one task, from its row, or from the log when it has none.

    `window` is the window of the run, in tokens. It is only used for a task without a row.
    """
    async with sessions() as session:
        row = (
            await session.execute(
                select(Task.compactions, Task.max_fraction).where(Task.id == task_id)
            )
        ).first()
        if row is not None:
            return ContextTotals(compactions=row.compactions, max_fraction=row.max_fraction)
        fullest = (
            await session.execute(
                select(
                    func.max(Turn.input_tokens + Turn.cache_read_tokens + Turn.cache_write_tokens)
                ).where(Turn.task_id == task_id, Turn.role == CODER)
            )
        ).scalar_one()
        if fullest is None:
            return None
        compactions = (
            await session.execute(
                select(func.count(distinct(Event.payload))).where(
                    Event.task_id == task_id, Event.type == COMPACTION_EVENT
                )
            )
        ).scalar_one()
    share = fullest / window if window else None
    return ContextTotals(compactions=compactions, max_fraction=share)
