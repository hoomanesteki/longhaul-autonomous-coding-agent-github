"""Read Temporal's history for the time a run took to start again (LH-904).

Resume latency is the time from the end of a kill to the start of the next activity, taken from
the timestamps that the server wrote into the workflow's history. The log of the worker would
not do: it dies with a SIGKILL, and it has nothing to say about a restart of the server.

A retried activity writes its ActivityTaskStarted event when it has run, and the event carries the
time the retry started, not the time it ended. So the start of a retry after a killed worker is
the start of the new attempt.

A run that hands over to a new one (Continue-As-New) has one history for each run, so the chain
is followed back from the latest run through `continued_execution_run_id`.

The times are the server's. In Docker Desktop the server runs in a VM whose clock can differ from
this machine's by a little, so `clock_offset_s` puts the two side by side.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta

from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent

HistoryReader = Callable[[str | None], Awaitable[list[HistoryEvent]]]


def activity_starts(events: Iterable[HistoryEvent]) -> list[datetime]:
    """The times of the ActivityTaskStarted events, in the order of the events."""
    return [
        event.event_time.ToDatetime(tzinfo=UTC)
        for event in events
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED
    ]


def resume_latency_s(
    starts: Sequence[datetime], struck_at: datetime, clock_offset_s: float = 0.0
) -> float | None:
    """Seconds from the end of a kill to the first activity that starts after it.

    `struck_at` is on this machine's clock and `starts` are on the server's, which is
    `clock_offset_s` ahead. None when no activity started after the kill, which is a run that
    ended with it.
    """
    moment = struck_at + timedelta(seconds=clock_offset_s)
    later = [start for start in starts if start > moment]
    if not later:
        return None
    return (min(later) - moment).total_seconds()


def clock_offset_s(other_s: float, before_s: float, after_s: float) -> float:
    """How far ahead of this machine another clock is, from one reading of that clock.

    The other clock was read between `before_s` and `after_s` on this machine's, so its reading
    is compared with the middle of the two.
    """
    return other_s - (before_s + after_s) / 2


async def chain_history(read: HistoryReader) -> list[HistoryEvent]:
    """The events of every run of a task, the oldest run first.

    `read(None)` gives the latest run, and `read(run_id)` the run with that id. The first event
    of a run names the run it continued, if it continued one.
    """
    runs = [await read(None)]
    while True:
        first = runs[0][0] if runs[0] else None
        previous = (
            first.workflow_execution_started_event_attributes.continued_execution_run_id
            if first is not None
            else ""
        )
        if not previous:
            break
        runs.insert(0, await read(previous))
    return [event for run in runs for event in run]
