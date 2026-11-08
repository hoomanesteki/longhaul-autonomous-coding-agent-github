"""The budget rules of a task: which limit a model call would cross, and what to say about it.

A task has four limits (`Budgets` in contracts.py): dollars and turns for the task, wall-clock
minutes, and dollars per repo and day. A top-up (`/longhaul budget`) raises the first two for
the one task. Before every model turn the workflow asks whether the call fits.

* `check` judges the three limits that need the ledger or the turn count. The `check_budget`
  activity runs it with the sums it reads, because the workflow may not read a database.
* `wall_clock_exceeded` and `elapsed` judge the clock. The workflow does that itself from
  `workflow.now()`, so a replay sees the same time.
* `day_start` and `next_midnight` bound the day of the repo. A daily limit is the one a person
  cannot lift, so the task waits for the new day instead of for a signal.

The worst case of the next call is what is compared, not what the last call cost: the check runs
before the call, and it is better to pause a little early than to find out afterwards. The
projected cost is the input of the last request at full price plus `max_tokens` of output.

Every function is pure. It reads no clock and no database, so the workflow may import it
(invariant I1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from longhaul.contracts import Budgets, Outcome, OutcomeKind
from longhaul.roles.stuck import waited_text

__all__ = [
    "BudgetNumbers",
    "BudgetReason",
    "BudgetVerdict",
    "check",
    "day_start",
    "elapsed",
    "exhausted_outcome",
    "next_midnight",
    "pause_reason",
    "unfit_outcome",
    "wall_clock_exceeded",
]

# The ledger holds millionths of a dollar, so the numbers do too.
_ROUND = Decimal("0.000001")


class BudgetReason(StrEnum):
    """The limit a call would cross. The values are what the events and the comment say."""

    TASK_USD = "task_usd"
    TASK_TURNS = "task_turns"
    WALL_CLOCK = "wall_clock"
    DAILY_REPO_USD = "daily_repo_usd"


class BudgetNumbers(BaseModel):
    """The numbers behind a verdict, for the comment and for the `paused` event.

    Money is a float here because the numbers cross the activity boundary as JSON and are shown,
    not added up. The limits are those in force: the configured limit plus any top-up.
    """

    spent_task_usd: float = Field(default=0.0, ge=0)
    projected_usd: float = Field(default=0.0, ge=0)
    task_usd_limit: float = Field(default=0.0, ge=0)
    turns_done: int = Field(default=0, ge=0)
    turns_limit: int = Field(default=0, ge=0)
    spent_repo_today_usd: float = Field(default=0.0, ge=0)
    daily_repo_usd_limit: float = Field(default=0.0, ge=0)
    elapsed_minutes: float = Field(default=0.0, ge=0)
    wall_clock_minutes_limit: int = Field(default=0, ge=0)


class BudgetVerdict(BaseModel):
    """What `check_budget` answers: whether the next call fits, and if not, which limit stops it.

    `can_wait` is False for a daily limit that the next call would cross on an empty day: waiting
    for midnight would bring the same answer, so the workflow ends the task instead.
    """

    ok: bool
    reason: BudgetReason | None = None
    numbers: BudgetNumbers = Field(default_factory=BudgetNumbers)
    can_wait: bool = True


def _money(value: Decimal) -> float:
    return float(value.quantize(_ROUND))


def _dollars(value: float) -> Decimal:
    # repr() and not the float itself: Decimal(0.1) is the binary number, Decimal("0.1") is 0.1.
    return Decimal(repr(value))


def check(
    budgets: Budgets,
    *,
    extra_usd: float,
    extra_turns: int,
    turn_index: int,
    spent_task: Decimal,
    spent_repo_today: Decimal,
    projected: Decimal,
) -> BudgetVerdict:
    """Judge the task limits and the daily limit of the repo for the next call.

    The limits are named in the order a person can act on them: the turns, then the task's
    dollars, then the repo's day. `turn_index` counts the turns done, so a task with a limit of
    ten stops when it has done ten. The dollars compare what was spent plus the worst case of the
    next call with the limit plus the top-ups.
    """
    task_limit = _dollars(budgets.task_usd) + _dollars(extra_usd)
    turn_limit = budgets.task_turns + extra_turns
    daily_limit = _dollars(budgets.daily_repo_usd)
    numbers = BudgetNumbers(
        spent_task_usd=_money(spent_task),
        projected_usd=_money(projected),
        task_usd_limit=_money(task_limit),
        turns_done=turn_index,
        turns_limit=turn_limit,
        spent_repo_today_usd=_money(spent_repo_today),
        daily_repo_usd_limit=_money(daily_limit),
    )
    if turn_index >= turn_limit:
        return BudgetVerdict(ok=False, reason=BudgetReason.TASK_TURNS, numbers=numbers)
    if spent_task + projected > task_limit:
        return BudgetVerdict(ok=False, reason=BudgetReason.TASK_USD, numbers=numbers)
    if spent_repo_today + projected > daily_limit:
        return BudgetVerdict(
            ok=False,
            reason=BudgetReason.DAILY_REPO_USD,
            numbers=numbers,
            can_wait=projected <= daily_limit,
        )
    return BudgetVerdict(ok=True, numbers=numbers)


# ---------- the clock ----------
def elapsed(started_at: datetime, now: datetime, *, paused_s: float) -> timedelta:
    """The time a task has worked: since it started, without the time it waited for a person.

    `paused_s` is the seconds the task spent in pauses (the pause for a hint, for a top-up, for
    midnight). A person who answers after three hours has not used three hours of the task.
    """
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)  # the contract says a datetime, not which
    return max(now - started_at - timedelta(seconds=paused_s), timedelta(0))


def wall_clock_exceeded(worked: timedelta, minutes: int) -> bool:
    """True once the task has worked longer than `minutes`. Exactly at the limit is inside it."""
    return worked > timedelta(minutes=minutes)


# ---------- the day of the repo ----------
def day_start(now: datetime) -> datetime:
    """00:00 UTC of the day that `now` is in."""
    utc = now.astimezone(UTC)
    return utc.replace(hour=0, minute=0, second=0, microsecond=0)


def next_midnight(now: datetime) -> datetime:
    """The next 00:00 UTC after `now`. At midnight itself that is a day later."""
    return day_start(now) + timedelta(days=1)


# ---------- the words ----------
_LIMIT_NAMES = {
    BudgetReason.TASK_USD: "task usd",
    BudgetReason.TASK_TURNS: "task turns",
    BudgetReason.WALL_CLOCK: "wall clock",
    BudgetReason.DAILY_REPO_USD: "daily repo usd",
}


def pause_reason(stop: BudgetVerdict) -> str:
    """What `status()` says a paused task waits for."""
    reason = stop.reason
    if reason is None:
        raise ValueError("a verdict that is ok has no pause to describe")
    if reason is BudgetReason.DAILY_REPO_USD:
        return f"waiting for midnight UTC: {_LIMIT_NAMES[reason]}"
    return f"waiting for a budget top-up: {_LIMIT_NAMES[reason]}"


def usd(value: float) -> str:
    """Dollars as people write them: two places, more only where a cent would hide the amount."""
    text = f"{value:.4f}".rstrip("0")
    whole, _, cents = text.partition(".")
    return f"${whole}.{cents.ljust(2, '0')}"


def _count(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def _spent_text(stop: BudgetVerdict) -> str:
    """The sentence start for a task limit: what the budget was, in the words of the outcome."""
    numbers = stop.numbers
    match stop.reason:
        case BudgetReason.TASK_USD:
            return f"task budget of {usd(numbers.task_usd_limit)}"
        case BudgetReason.TASK_TURNS:
            return f"turn budget of {_count(numbers.turns_limit, 'turn')}"
        case BudgetReason.WALL_CLOCK:
            return f"time budget of {_count(numbers.wall_clock_minutes_limit, 'minute')}"
        case _:
            raise ValueError(f"{stop.reason} is not a limit that a top-up can lift")


def exhausted_outcome(stop: BudgetVerdict, wait: timedelta) -> Outcome:
    """The end of a task whose task limit nobody lifted within `wait`."""
    return Outcome(
        kind=OutcomeKind.BUDGET_EXHAUSTED,
        summary=f"{_spent_text(stop)} is spent: no top-up in {waited_text(wait)}",
    )


def unfit_outcome(stop: BudgetVerdict) -> Outcome:
    """The end of a task whose next call is bigger than a whole day of the repo's budget."""
    limit = usd(stop.numbers.daily_repo_usd_limit)
    return Outcome(
        kind=OutcomeKind.BUDGET_EXHAUSTED,
        summary=f"daily budget of {limit} for the repo is smaller than the next call",
    )
