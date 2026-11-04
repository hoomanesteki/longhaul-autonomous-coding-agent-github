"""The cost ledger: one row for every model call, priced when it is written.

Invariant I6 says that every provider call writes a `cost_ledger` row, so what a task and a repo
spent can be added up from one table. `Ledger.record` prices the usage that the provider
reported, stores the row and returns the dollars. An activity that calls a provider records the
call as soon as it returns, before anything else that can fail. The call is then on the ledger
when the activity fails afterwards and Temporal runs it again.

The run that follows is another call and gets a row of its own. `attempt` is the Temporal
attempt number of the activity, so a retried llm_turn shows as attempts 1 and 2 (plan section
4.1, F1). Rows are never updated, and one turn can have several with the same attempt: a reply
cut off at max_tokens is asked again, and a paused reply is continued, each by a new run of the
activity that starts at attempt 1. A call that failed before the provider billed it has no usage
and no row, as in the `turns` table.

After each call `SqlLedger` sets `tasks.cost_usd` to the sum of the task's rows. The column holds
ten thousandths of a dollar and the rows millionths, so adding each call to it would round every
call and drift from the ledger. The sum is rounded once instead. A task with no row yet, as in a
local run, is not an error: its calls are on the ledger, and the total catches up at the first
call after the row exists.

`MemoryLedger` keeps the rows in a list for unit tests, and `NoLedger` records nothing, for a
caller with no database.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from longhaul.contracts import Usage
from longhaul.cost.pricing import Pricing
from longhaul.db.models import CostLedgerRow, Task

# The `usd` column holds numeric(10,6): millionths of a dollar.
_MILLIONTH = Decimal("0.000001")


def _priced(pricing: Pricing, usage: Usage, model: str) -> Decimal:
    """What a call cost, rounded to what the column holds.

    It is rounded here and not by the database, so that the amount `record` returns is the
    amount that was stored. Half rounds up, as Postgres rounds.
    """
    return pricing.cost(usage, model).quantize(_MILLIONTH, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LedgerEntry:
    """One row of the ledger, as the memory ledger keeps it."""

    task_id: str
    repo: str
    model: str
    turn_index: int
    attempt: int
    usage: Usage
    usd: Decimal


class Ledger(Protocol):
    async def record(
        self, task_id: str, repo: str, model: str, turn_index: int, attempt: int, usage: Usage
    ) -> Decimal:
        """Write the row for one call and return its cost in dollars.

        A model without a price raises PricingError before anything is written.
        """
        ...


class NoLedger:
    """A ledger that writes nothing and costs nothing, for a caller with no database."""

    async def record(
        self, task_id: str, repo: str, model: str, turn_index: int, attempt: int, usage: Usage
    ) -> Decimal:
        return Decimal(0)


class MemoryLedger:
    """The rows in memory, for tests. Public, so that a test can read what was written."""

    def __init__(self, pricing: Pricing) -> None:
        self._pricing = pricing
        self.entries: list[LedgerEntry] = []

    async def record(
        self, task_id: str, repo: str, model: str, turn_index: int, attempt: int, usage: Usage
    ) -> Decimal:
        usd = _priced(self._pricing, usage, model)
        self.entries.append(LedgerEntry(task_id, repo, model, turn_index, attempt, usage, usd))
        return usd


class SqlLedger:
    """Writes to Postgres through an async session factory."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession], pricing: Pricing) -> None:
        self._sessions = sessions
        self._pricing = pricing

    async def record(
        self, task_id: str, repo: str, model: str, turn_index: int, attempt: int, usage: Usage
    ) -> Decimal:
        usd = _priced(self._pricing, usage, model)
        row = insert(CostLedgerRow).values(
            task_id=task_id,
            repo=repo,
            model=model,
            turn_index=turn_index,
            attempt=attempt,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            usd=usd,
        )
        total = (
            select(func.coalesce(func.sum(CostLedgerRow.usd), 0))
            .where(CostLedgerRow.task_id == task_id)
            .scalar_subquery()
        )
        async with self._sessions.begin() as session:
            # The task row is locked first. Two calls of one task then take turns, and the
            # second one sums the rows after the first one's row is in. Without the lock both
            # could sum before either row was visible, and one call would be missing from the
            # total. A task with no row locks nothing, and the update below changes nothing.
            await session.execute(select(Task.id).where(Task.id == task_id).with_for_update())
            await session.execute(row)
            await session.execute(update(Task).where(Task.id == task_id).values(cost_usd=total))
        return usd
