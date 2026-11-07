"""The Temporal Schedule that runs the poller, and the calls that manage it (LH-307).

`longhaul poller install` creates the Schedule `longhaul-poller`. It starts PollerWorkflow every
`poller.interval_seconds` with overlap policy SKIP, so a tick that comes while a poll is still
open is dropped and two polls never run at once (ADR-0008, and ADR-0004 appendix section 7).

Install can be run again at any time and changes nothing when nothing differs. That is what
makes it safe to put in a deploy script. It compares only what it owns (`fingerprint`): the
action, the interval, the overlap policy and the catch-up window. The state is left to the
operator. A Schedule that someone paused stays paused when install changes its interval, and an
install that finds the Schedule as it wants it sends no update, so `last_updated_at` does not
move.

`pause`, `resume` and `trigger` act on the Schedule by its id and raise ScheduleMissingError,
with the command that fixes it, when there is none. A trigger starts one poll at once. It
follows the overlap policy like any tick, so it does nothing while a poll is open, and it works
on a paused Schedule too.

Every function takes the id of the Schedule, which tests change so that they never touch the
real one. The runs of a Schedule are named after it, `<id>-run`, for the same reason.

This module is for the command line and for tests. The worker never imports it.
"""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Iterator
from datetime import timedelta
from enum import StrEnum
from typing import Any

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.service import RPCError, RPCStatusCode

from longhaul.durable import acts
from longhaul.durable.workflows.poller import PollerWorkflow

__all__ = [
    "Installed",
    "ScheduleMissingError",
    "fingerprint",
    "install_poller",
    "pause_poller",
    "poller_schedule",
    "reconcile",
    "resume_poller",
    "trigger_poller",
]

PAUSE_NOTE = "paused by `longhaul poller pause`"
RESUME_NOTE = "resumed by `longhaul poller resume`"


class Installed(StrEnum):
    """What `install_poller` did."""

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


class ScheduleMissingError(ValueError):
    """The Schedule to manage does not exist. A ValueError, so the CLI reports it in a line."""


def poller_schedule(
    interval_seconds: int, task_queue: str, *, schedule_id: str = acts.POLLER_SCHEDULE_ID
) -> Schedule:
    """The Schedule that install wants: one poll every `interval_seconds` on `task_queue`."""
    if interval_seconds < 1:
        raise ValueError(f"the poll interval must be at least 1 second, not {interval_seconds}")
    if not task_queue:
        raise ValueError("the poller needs a task queue")
    return Schedule(
        action=ScheduleActionStartWorkflow(
            PollerWorkflow.run,
            id=f"{schedule_id}-run",
            task_queue=task_queue,
            execution_timeout=acts.POLLER_RUN_TIMEOUT,
        ),
        spec=ScheduleSpec(
            intervals=[ScheduleIntervalSpec(every=timedelta(seconds=interval_seconds))]
        ),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP, catchup_window=acts.POLLER_CATCHUP_WINDOW
        ),
    )


def fingerprint(wanted: Schedule) -> tuple[Any, ...]:
    """The parts of a Schedule that install owns, in a form that compares with ==.

    A Schedule that the server describes holds the workflow as a name and the intervals with an
    offset of zero where the request left it out, so both sides are brought to the same form.
    """
    action = wanted.action
    if not isinstance(action, ScheduleActionStartWorkflow):
        return ("not a workflow action",)
    spec = wanted.spec
    return (
        action.workflow,
        action.task_queue,
        action.id,
        action.execution_timeout,
        tuple((i.every, i.offset or timedelta(0)) for i in spec.intervals),
        tuple(spec.calendars),
        tuple(spec.cron_expressions),
        wanted.policy.overlap,
        wanted.policy.catchup_window,
    )


def reconcile(current: Schedule, wanted: Schedule) -> Schedule | None:
    """The Schedule to write so that `current` becomes `wanted`, or None when it already is.

    The state of `current`, its pause and its note, is kept: whether the poller runs is the
    operator's decision and not part of what install owns.
    """
    if fingerprint(current) == fingerprint(wanted):
        return None
    return dataclasses.replace(wanted, state=current.state)


async def install_poller(
    client: Client,
    *,
    interval_seconds: int,
    task_queue: str,
    schedule_id: str = acts.POLLER_SCHEDULE_ID,
) -> Installed:
    """Create the Schedule, or bring an existing one to the wanted interval and queue."""
    wanted = poller_schedule(interval_seconds, task_queue, schedule_id=schedule_id)
    try:
        await client.create_schedule(schedule_id, wanted)
    except ScheduleAlreadyRunningError:
        pass
    else:
        return Installed.CREATED

    changed = False

    def update(found: ScheduleUpdateInput) -> ScheduleUpdate | None:
        nonlocal changed
        write = reconcile(found.description.schedule, wanted)
        if write is None:
            return None  # an update that returns None is not sent
        changed = True
        return ScheduleUpdate(schedule=write)

    await client.get_schedule_handle(schedule_id).update(update)
    return Installed.UPDATED if changed else Installed.UNCHANGED


@contextlib.contextmanager
def _existing(schedule_id: str) -> Iterator[None]:
    try:
        yield
    except RPCError as exc:
        if exc.status is not RPCStatusCode.NOT_FOUND:
            raise
        raise ScheduleMissingError(
            f"the schedule {schedule_id} does not exist. Create it with `longhaul poller install`"
        ) from None


async def pause_poller(
    client: Client, *, note: str = "", schedule_id: str = acts.POLLER_SCHEDULE_ID
) -> None:
    """Stop the ticks. A poll that is open runs to its end."""
    with _existing(schedule_id):
        await client.get_schedule_handle(schedule_id).pause(note=note or PAUSE_NOTE)


async def resume_poller(
    client: Client, *, note: str = "", schedule_id: str = acts.POLLER_SCHEDULE_ID
) -> None:
    """Start the ticks again."""
    with _existing(schedule_id):
        await client.get_schedule_handle(schedule_id).unpause(note=note or RESUME_NOTE)


async def trigger_poller(client: Client, *, schedule_id: str = acts.POLLER_SCHEDULE_ID) -> None:
    """Ask for one poll now. The overlap policy applies, so an open poll makes it a no-op."""
    with _existing(schedule_id):
        await client.get_schedule_handle(schedule_id).trigger()
