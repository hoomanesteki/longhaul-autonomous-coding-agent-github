"""The budget rules of a task, as pure functions (LH-702).

A task has four limits: dollars and turns for the task, wall-clock minutes, and dollars per repo
and day. `check` judges the three that need the ledger or the turn count, the workflow judges the
clock itself, and both give a verdict that names the limit and carries the numbers for the
comment. Nothing here reads a clock or a database, so the workflow may import it.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from longhaul.cost.budget import BudgetReason

from longhaul.contracts import Budgets, OutcomeKind
from longhaul.cost import budget

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

LIMITS = Budgets(task_usd=5.0, task_turns=150, task_wall_minutes=240, daily_repo_usd=25.0)


def judge(**changes: object) -> budget.BudgetVerdict:
    """A task well inside every limit, with `changes` applied."""
    values: dict[str, object] = {
        "extra_usd": 0.0,
        "extra_turns": 0,
        "turn_index": 10,
        "spent_task": Decimal("1.00"),
        "spent_repo_today": Decimal("2.00"),
        "projected": Decimal("0.05"),
    }
    values.update(changes)
    return budget.check(LIMITS, **values)  # type: ignore[arg-type]


# ---------- the limits that check judges ----------
def test_a_task_inside_every_limit_is_ok() -> None:
    verdict = judge()
    assert verdict.ok and verdict.reason is None
    assert verdict.numbers.spent_task_usd == 1.0
    assert verdict.numbers.projected_usd == 0.05


def test_the_task_dollar_limit_counts_the_worst_case_of_the_next_call() -> None:
    # 4.96 spent and 0.05 projected is 5.01, one cent over the limit of 5.00.
    verdict = judge(spent_task=Decimal("4.96"))
    assert not verdict.ok and verdict.reason is BudgetReason.TASK_USD
    assert verdict.numbers.task_usd_limit == 5.0
    assert verdict.numbers.spent_task_usd == 4.96
    # Exactly at the limit is still inside it.
    assert judge(spent_task=Decimal("4.95")).ok


def test_a_top_up_raises_the_task_dollar_limit() -> None:
    assert judge(spent_task=Decimal("4.96"), extra_usd=1.0).ok
    verdict = judge(spent_task=Decimal("5.96"), extra_usd=1.0)
    assert verdict.reason is BudgetReason.TASK_USD and verdict.numbers.task_usd_limit == 6.0


def test_the_turn_limit_is_reached_when_the_turns_done_equal_it() -> None:
    assert judge(turn_index=149).ok
    verdict = judge(turn_index=150)
    assert not verdict.ok and verdict.reason is BudgetReason.TASK_TURNS
    assert (verdict.numbers.turns_done, verdict.numbers.turns_limit) == (150, 150)


def test_a_top_up_raises_the_turn_limit() -> None:
    assert judge(turn_index=150, extra_turns=20).ok
    verdict = judge(turn_index=170, extra_turns=20)
    assert verdict.reason is BudgetReason.TASK_TURNS and verdict.numbers.turns_limit == 170


def test_the_daily_limit_adds_the_projection_to_what_the_repo_spent_today() -> None:
    verdict = judge(spent_repo_today=Decimal("24.96"))
    assert not verdict.ok and verdict.reason is BudgetReason.DAILY_REPO_USD
    assert verdict.numbers.spent_repo_today_usd == 24.96
    assert verdict.numbers.daily_repo_usd_limit == 25.0
    assert verdict.can_wait, "tomorrow the same call fits"
    assert judge(spent_repo_today=Decimal("24.95")).ok


def test_a_call_that_alone_is_over_the_daily_limit_cannot_wait_for_midnight() -> None:
    only_daily = budget.check(
        Budgets(task_usd=100.0, daily_repo_usd=25.0),
        extra_usd=0.0,
        extra_turns=0,
        turn_index=0,
        spent_task=Decimal("0"),
        spent_repo_today=Decimal("0"),
        projected=Decimal("26.00"),
    )
    assert only_daily.reason is BudgetReason.DAILY_REPO_USD
    assert not only_daily.can_wait


def test_the_turn_limit_is_named_before_the_dollar_limit_when_both_are_reached() -> None:
    verdict = judge(turn_index=150, spent_task=Decimal("5.00"))
    assert verdict.reason is BudgetReason.TASK_TURNS


def test_the_task_limits_are_named_before_the_daily_limit() -> None:
    verdict = judge(spent_task=Decimal("5.00"), spent_repo_today=Decimal("30.00"))
    assert verdict.reason is BudgetReason.TASK_USD


# ---------- the wall clock ----------
def test_the_clock_runs_from_the_start_and_leaves_out_the_time_spent_waiting_for_a_person() -> None:
    start = datetime(2026, 11, 3, 8, 0, tzinfo=UTC)
    now = start + timedelta(minutes=300)
    assert budget.elapsed(start, now, paused_s=0) == timedelta(minutes=300)
    assert budget.elapsed(start, now, paused_s=3600 * 2) == timedelta(minutes=180)
    assert budget.elapsed(start, now, paused_s=3600 * 9) == timedelta(0), "never below zero"


def test_the_wall_clock_limit_is_passed_after_the_minutes_not_at_them() -> None:
    assert not budget.wall_clock_exceeded(timedelta(minutes=240), 240)
    assert budget.wall_clock_exceeded(timedelta(minutes=240, seconds=1), 240)


# ---------- the day ----------
def test_the_day_of_the_repo_starts_at_midnight_utc() -> None:
    now = datetime(2026, 11, 3, 17, 45, 12, tzinfo=UTC)
    assert budget.day_start(now) == datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    assert budget.next_midnight(now) == datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


def test_a_moment_at_midnight_belongs_to_the_new_day() -> None:
    midnight = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
    assert budget.day_start(midnight) == midnight
    assert budget.next_midnight(midnight) == midnight + timedelta(days=1)


def test_a_time_in_another_zone_is_read_as_utc() -> None:
    zone = datetime.fromisoformat("2026-11-03T23:30:00-05:00")  # 04:30 UTC on the 4th
    assert budget.day_start(zone) == datetime(2026, 11, 4, 0, 0, tzinfo=UTC)


# ---------- the words ----------
def verdict_for(reason: BudgetReason) -> budget.BudgetVerdict:
    numbers = budget.BudgetNumbers(
        spent_task_usd=4.96,
        projected_usd=0.05,
        task_usd_limit=5.0,
        turns_done=150,
        turns_limit=150,
        spent_repo_today_usd=24.0,
        daily_repo_usd_limit=25.0,
        elapsed_minutes=241.0,
        wall_clock_minutes_limit=240,
    )
    return budget.BudgetVerdict(ok=False, reason=reason, numbers=numbers)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (BudgetReason.TASK_USD, "task budget of $5.00 is spent: no top-up in 24 hours"),
        (BudgetReason.TASK_TURNS, "turn budget of 150 turns is spent: no top-up in 24 hours"),
        (
            BudgetReason.WALL_CLOCK,
            "time budget of 240 minutes is spent: no top-up in 24 hours",
        ),
    ],
)
def test_a_task_limit_that_nobody_tops_up_ends_with_budget_exhausted(
    reason: BudgetReason, expected: str
) -> None:
    outcome = budget.exhausted_outcome(verdict_for(reason), timedelta(hours=24))
    assert outcome.kind is OutcomeKind.BUDGET_EXHAUSTED
    assert outcome.summary == expected


def test_the_wait_is_named_in_the_unit_that_holds_it() -> None:
    outcome = budget.exhausted_outcome(verdict_for(BudgetReason.TASK_USD), timedelta(seconds=90))
    assert outcome.summary.endswith("no top-up in 90 seconds")


def test_a_daily_limit_that_a_new_day_cannot_help_ends_with_budget_exhausted() -> None:
    outcome = budget.unfit_outcome(verdict_for(BudgetReason.DAILY_REPO_USD))
    assert outcome.kind is OutcomeKind.BUDGET_EXHAUSTED
    assert outcome.summary == "daily budget of $25.00 for the repo is smaller than the next call"


def test_the_status_says_what_the_task_waits_for() -> None:
    assert budget.pause_reason(verdict_for(BudgetReason.TASK_USD)) == (
        "waiting for a budget top-up: task usd"
    )
    assert budget.pause_reason(verdict_for(BudgetReason.DAILY_REPO_USD)) == (
        "waiting for midnight UTC: daily repo usd"
    )
