"""The comment that a task leaves when a budget stops it (LH-702).

The text comes from templates/budget_comment.md.j2. A maintainer reads it on the issue and answers
with one command, so it has to give the numbers and the exact commands. The commands are checked
against the parser of the poller: a comment that advertised a command the poller refuses would
leave the task waiting for nothing.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from longhaul.cost.budget import BudgetNumbers, BudgetReason
from longhaul.github.budget_comment import render_budget_comment
from longhaul.github.commands import CommandKind, parse_command

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
WORD_LIMIT = 250


def words(text: str) -> int:
    return len(text.split())


BUDGET_NUMBERS = BudgetNumbers(
    spent_task_usd=4.9612,
    projected_usd=0.05,
    task_usd_limit=5.0,
    turns_done=150,
    turns_limit=150,
    spent_repo_today_usd=24.0,
    daily_repo_usd_limit=25.0,
    elapsed_minutes=241.5,
    wall_clock_minutes_limit=240,
)
MIDNIGHT = datetime(2026, 11, 4, tzinfo=UTC)


def budget_text(
    reason: BudgetReason,
    pause: int = 1,
    wait: timedelta | None = timedelta(hours=24),
    resume_at: datetime | None = None,
) -> str:
    return render_budget_comment(
        reason, BUDGET_NUMBERS, pause, wait=wait, resume_at=resume_at, templates=TEMPLATES
    )


def commands_in(text: str) -> list[str]:
    """The text of every code span that starts with the command prefix."""
    return [span for span in re.findall(r"`([^`]+)`", text) if span.startswith("/longhaul")]


def test_the_task_dollar_comment_gives_the_numbers_and_the_exact_commands() -> None:
    text = budget_text(BudgetReason.TASK_USD)
    assert text.splitlines()[0].startswith("## Longhaul paused: task budget")
    assert "$4.9612" in text and "$0.05" in text and "$5.00" in text
    assert commands_in(text) == ["/longhaul budget +2", "/longhaul cancel"]
    assert "24 hours" in text and "budget_exhausted" in text


def test_the_turn_comment_gives_the_turns_and_a_command_that_adds_some() -> None:
    text = budget_text(BudgetReason.TASK_TURNS)
    assert "150 of 150 turns" in text
    assert commands_in(text) == ["/longhaul budget +1 turns +25", "/longhaul cancel"]


def test_the_wall_clock_comment_says_a_top_up_starts_a_new_window() -> None:
    text = budget_text(BudgetReason.WALL_CLOCK)
    assert "240 minutes" in text and "241" in text
    assert "new window" in text
    assert commands_in(text) == ["/longhaul budget +1", "/longhaul cancel"]


def test_the_daily_comment_says_when_the_task_goes_on_without_anyone() -> None:
    text = budget_text(BudgetReason.DAILY_REPO_USD, wait=None, resume_at=MIDNIGHT)
    assert "$24.00" in text and "$25.00" in text
    assert "2026-11-04 00:00 UTC" in text
    assert "goes on by itself" in text
    assert commands_in(text) == ["/longhaul cancel"], "a top-up does not raise the daily limit"


@pytest.mark.parametrize("reason", list(BudgetReason))
def test_every_command_in_a_budget_comment_is_one_the_poller_understands(
    reason: BudgetReason,
) -> None:
    wait = None if reason is BudgetReason.DAILY_REPO_USD else timedelta(hours=24)
    resume = MIDNIGHT if reason is BudgetReason.DAILY_REPO_USD else None
    for command in commands_in(budget_text(reason, wait=wait, resume_at=resume)):
        parsed = parse_command(command)
        assert parsed is not None, command
        assert parsed.kind in (CommandKind.BUDGET, CommandKind.CANCEL)


def test_the_budget_comment_is_short_and_the_same_every_time() -> None:
    text = budget_text(BudgetReason.TASK_USD)
    assert words(text) < WORD_LIMIT
    assert text == budget_text(BudgetReason.TASK_USD)


def test_the_title_has_the_pause_number() -> None:
    assert "pause 2" in budget_text(BudgetReason.TASK_USD, pause=2).splitlines()[0]


def test_a_task_limit_without_a_wait_is_refused() -> None:
    with pytest.raises(ValueError, match="wait"):
        budget_text(BudgetReason.TASK_USD, wait=None)


def test_a_daily_limit_without_a_time_is_refused() -> None:
    with pytest.raises(ValueError, match="resume_at"):
        budget_text(BudgetReason.DAILY_REPO_USD, wait=None, resume_at=None)
