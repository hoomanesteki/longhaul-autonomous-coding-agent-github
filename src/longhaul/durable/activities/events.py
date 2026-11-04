"""The record_event activity: one row in a task's event log, written for the workflow.

The workflow cannot reach Postgres, so it asks for each event through this activity. The call is
local (acts.RECORD_EVENT): it runs inside the worker that holds the workflow, because it is one
insert and a task queue round trip would cost more than the insert. Activities that write events
of their own, such as ensure_sandbox with `sandbox_recreated`, use the recorder directly.

An event is a log line. The recorder writes it every time it is asked, so a retry of this
activity after a write that went through adds a second row. That is rare for a single insert, and
a log that says a thing happened twice is a smaller fault than one that lost it.

Metrics (LH-704). A compaction and a stuck event move their counters (telemetry/metrics.py)
after the row is written, so those metrics follow the table. The outcome event, which a run sends
as its last act (acts.OUTCOME_EVENT), counts the task as finished and takes it out of the active
tasks before the row is written. A run that ends because the database is down is one the
dashboard has to show, and its row is the very write that fails. The count is once per workflow
run: a retry of this activity, or a workflow task that is retried, runs it again for the same run,
and the second call writes a second row of the log and no second count.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from temporalio import activity

from longhaul.db.records import Recorder
from longhaul.durable import acts
from longhaul.durable.activities.support import small
from longhaul.telemetry import metrics


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
        if inp.type == acts.OUTCOME_EVENT:
            payload = inp.payload
            metrics.current().finished(
                activity.info().workflow_run_id or inp.task_id,
                inp.task_id,
                str(payload.get("repo", metrics.UNKNOWN)),
                str(payload.get("kind", metrics.UNKNOWN)),
            )
            await self._recorder.record_event(inp.task_id, inp.type, inp.payload)
            return
        await self._recorder.record_event(inp.task_id, inp.type, inp.payload)
        metrics.current().count_event(inp.type, inp.payload)
