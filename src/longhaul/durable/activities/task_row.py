"""The record_task_state activity: the row of a task in `tasks`, kept current for the console.

The workflow cannot write to Postgres, so it asks for each change of the row through this
activity (LH-891). The poller writes the row once, at pickup, and the console lists tasks from it.
Without these writes the list showed every task as queued with no turns and no cost, and the
Needs you filter was empty while two tasks waited for a person.

The body is one upsert through the task store (db/tasks.py, `record_state`). A task started with
`longhaul run` has no poller and no row, and its first write creates one, so local runs show in
the console too. The upsert changes the phase, the status, the turns, the cost, the outcome, the
pull request and the end time. It leaves the title, the settings, the start time and the comment
cursor alone, which belong to the poller.

A repeat of a write is harmless: the same state written twice leaves the row as it was.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from temporalio import activity

from longhaul.db.tasks import TaskState, TaskStore
from longhaul.durable import acts
from longhaul.durable.activities.support import small


class TaskRowActivities:
    """The record_task_state activity and the store it writes to. Every worker registers it."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.record_task_state]

    @activity.defn(name=acts.RECORD_TASK_STATE.name)
    async def record_task_state(self, inp: acts.RecordTaskStateInput) -> None:
        inp = small(inp)
        await self._store.record_state(
            TaskState(
                task_id=inp.task_id,
                repo=inp.repo,
                issue_number=inp.issue_number,
                title=inp.title,
                settings=inp.settings.model_dump(mode="json"),
                phase=inp.phase.value,
                turns=inp.turns,
                cost_usd=inp.cost_usd,
                outcome=None if inp.outcome is None else inp.outcome.value,
                pr_url=inp.pr_url,
                ended_at=inp.ended_at,
            )
        )
