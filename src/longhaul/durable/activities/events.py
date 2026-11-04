"""The record_event activity: one row in a task's event log, written for the workflow.

The workflow cannot reach Postgres, so it asks for each event through this activity. The call is
local (acts.RECORD_EVENT): it runs inside the worker that holds the workflow, because it is one
insert and a task queue round trip would cost more than the insert. Activities that write events
of their own, such as ensure_sandbox with `sandbox_recreated`, use the recorder directly.

An event is a log line. The recorder writes it every time it is asked, so a retry of this
activity after a write that went through adds a second row. That is rare for a single insert, and
a log that says a thing happened twice is a smaller fault than one that lost it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from temporalio import activity

from longhaul.db.records import Recorder
from longhaul.durable import acts
from longhaul.durable.activities.support import small


class EventActivities:
    """The record_event activity and the recorder it writes to."""

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.record_event]

    @activity.defn(name=acts.RECORD_EVENT.name)
    async def record_event(self, inp: acts.RecordEventInput) -> None:
        inp = small(inp)
        await self._recorder.record_event(inp.task_id, inp.type, inp.payload)
