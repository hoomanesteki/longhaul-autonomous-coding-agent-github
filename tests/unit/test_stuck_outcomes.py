"""How a task ends when a person does not unblock it (LH-506)."""

from datetime import timedelta

import pytest

from longhaul.contracts import OutcomeKind, StuckReason, StuckSignal
from longhaul.roles import stuck

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

DAY = timedelta(hours=24)
LOOP = StuckSignal(reason=StuckReason.REPEATED_FAILURE, evidence=["pytest -q failed 3 times"])
ASKED = StuckSignal(reason=StuckReason.AGENT_ASKED, evidence=["Should add() take floats?"])


def test_three_pauses_are_all_a_task_gets() -> None:
    assert stuck.MAX_PAUSES == 3


def test_a_pause_nobody_answered_ends_stuck_and_says_how_long_it_waited() -> None:
    outcome = stuck.unanswered_outcome(LOOP, DAY)
    assert outcome.kind is OutcomeKind.STUCK
    assert outcome.pr_url is None
    assert "pytest -q failed 3 times" in outcome.summary
    assert "24 hours" in outcome.summary


def test_a_question_nobody_answered_keeps_the_question_in_the_summary() -> None:
    outcome = stuck.unanswered_outcome(ASKED, DAY)
    assert "Should add() take floats?" in outcome.summary


def test_a_task_that_is_stuck_after_its_last_pause_ends_stuck() -> None:
    outcome = stuck.pauses_used_outcome(LOOP)
    assert outcome.kind is OutcomeKind.STUCK
    assert "3" in outcome.summary and "pytest -q failed 3 times" in outcome.summary


def test_a_summary_is_cut_to_a_sensible_length() -> None:
    long = StuckSignal(reason=StuckReason.NO_PROGRESS, evidence=["x" * 5_000])
    assert len(stuck.unanswered_outcome(long, DAY).summary) <= stuck.MAX_SUMMARY_CHARS
    assert len(stuck.pauses_used_outcome(long).summary) <= stuck.MAX_SUMMARY_CHARS


def test_the_wait_is_written_in_the_unit_that_fits() -> None:
    def said(wait: timedelta) -> str:
        return stuck.unanswered_outcome(LOOP, wait).summary

    assert "no hint in 24 hours" in said(timedelta(hours=24))
    assert "no hint in 1 hour" in said(timedelta(hours=1))
    assert "no hint in 30 minutes" in said(timedelta(minutes=30))
    assert "no hint in 1 minute" in said(timedelta(minutes=1))
    assert "no hint in 90 seconds" in said(timedelta(seconds=90))
