"""The pure parts of how a task ends (LH-507): the trace of an error, and two short texts.

TaskWorkflow cannot format a long trace into an event or choose words for a partial pull request
by itself without these, and a replay needs them to give the same text every time.
"""

import pytest
from temporalio.exceptions import ActivityError, ApplicationError, RetryState

from longhaul.contracts import Outcome, OutcomeKind
from longhaul.durable import acts, wind_up

pytestmark = [pytest.mark.unit, pytest.mark.phase5]


def raised(error: BaseException) -> BaseException:
    try:
        raise error
    except BaseException as caught:
        return caught


def test_the_trace_names_the_error_and_where_it_came_from() -> None:
    trace = wind_up.error_trace(raised(ValueError("the sha is bad")))
    assert "ValueError: the sha is bad" in trace
    assert "test_wind_up.py" in trace


def test_the_trace_of_a_failed_activity_keeps_the_cause() -> None:
    cause = ApplicationError("push rejected", type="CheckpointError", non_retryable=True)
    failed = ActivityError(
        "Activity task failed",
        scheduled_event_id=5,
        started_event_id=6,
        identity="worker",
        activity_type="deliver",
        activity_id="1",
        retry_state=RetryState.NON_RETRYABLE_FAILURE,
    )
    failed.__cause__ = cause

    trace = wind_up.error_trace(raised(failed))

    assert "push rejected" in trace
    assert "ActivityError" in trace


def test_a_long_trace_is_cut_to_the_limit_and_keeps_its_end() -> None:
    error: BaseException | None = None
    for n in range(200):
        try:
            raise RuntimeError(f"failure number {n}") from error
        except RuntimeError as caught:
            error = caught
    assert error is not None

    trace = wind_up.error_trace(error)

    assert len(trace) <= acts.ERROR_TRACE_CHARS
    assert trace.startswith(wind_up.TRACE_CUT)
    assert trace.rstrip().endswith("RuntimeError: failure number 199")


def test_a_long_message_keeps_its_start() -> None:
    # A provider error can echo the whole request. What went wrong is at the front.
    error = raised(RuntimeError("push rejected: " + "x" * 50_000))

    trace = wind_up.error_trace(error)

    assert "push rejected: xxx" in trace
    assert wind_up.MESSAGE_CUT in trace
    assert len(trace) <= acts.ERROR_TRACE_CHARS


def test_a_short_trace_is_kept_whole() -> None:
    trace = wind_up.error_trace(raised(KeyError("sku-7")))
    assert not trace.startswith(wind_up.TRACE_CUT)
    assert "KeyError: 'sku-7'" in trace


def test_the_trace_fits_in_an_event_payload() -> None:
    trace = wind_up.error_trace(raised(RuntimeError("é" * 100_000)))
    assert len(trace.encode()) < 16_000


def test_the_draft_pull_request_says_the_work_is_partial_and_why() -> None:
    summary = wind_up.draft_summary(Outcome(kind=OutcomeKind.STUCK, summary="no hint in 24 hours"))
    assert summary == "Partial work. The task is stuck: no hint in 24 hours."


def test_the_draft_summary_is_cut() -> None:
    summary = wind_up.draft_summary(Outcome(kind=OutcomeKind.STUCK, summary="word " * 500))
    assert len(summary) <= wind_up.DRAFT_SUMMARY_CHARS
    assert summary.endswith("...")


def test_the_cleanup_reason_names_the_outcome() -> None:
    assert wind_up.cleanup_reason(OutcomeKind.STUCK) == "the task ended: stuck"
