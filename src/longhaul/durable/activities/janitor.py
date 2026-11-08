"""The clean_repos activity: the janitor that removes the bare repos of old tasks (LH-507).

A task keeps its bare repo after it ends, so a person can read the checkpoints of a stuck or
failed task. The repo is the largest thing a task leaves on the worker's disk, and nothing else
removes it, so the poller schedule runs this sweep after each poll. A repo goes when the task it
belongs to ended more than `acts.REPO_RETENTION` ago.

The end of a task is its `outcome` event (LH-704), which the workflow records as its last act.
The janitor trusts nothing else:

* A repo whose task has no outcome event is left alone. It may be the repo of a task that is
  waiting a day for a hint. A task that was terminated from outside never records an end, and its
  repo stays until someone removes it. A repo that stays is a smaller fault than one that a
  running task loses.
* A repo that is not named like a task is not listed by the store, so a stray folder is left
  alone too.

It works on the repos of the host it runs on. A worker with its own disk sweeps its own disk, and
the sweep is safe to run twice at once, because removing a repo that is gone changes nothing.
One repo that cannot be removed is counted and logged, and the sweep goes on to the next.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from temporalio import activity

from longhaul.durable import acts

log = logging.getLogger(__name__)


class Repos(Protocol):
    """The bare repos of a host: which tasks have one, and how to remove one."""

    async def task_ids(self) -> list[str]: ...

    async def cleanup_task(self, task_id: str) -> None: ...


class Ends(Protocol):
    """Where the end of a task is on record."""

    async def ended_before(self, task_ids: Sequence[str], cutoff: datetime) -> set[str]:
        """The ids among `task_ids` whose task ended before `cutoff`."""
        ...


class JanitorActivities:
    """clean_repos, with the repos it sweeps and the record of when tasks ended.

    `clock` is for tests. The worker passes none and gets the current time.
    """

    def __init__(
        self,
        repos: Repos,
        ends: Ends,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repos = repos
        self._ends = ends
        self._clock = clock

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.clean_repos]

    @activity.defn(name=acts.CLEAN_REPOS.name)
    async def clean_repos(self) -> acts.JanitorResult:
        found = await self._repos.task_ids()
        old = await self._ends.ended_before(found, self._clock() - acts.REPO_RETENTION)
        removed: list[str] = []
        failed = 0
        for task_id in found:
            if task_id not in old:
                continue
            try:
                await self._repos.cleanup_task(task_id)
            except Exception as exc:  # one stubborn folder must not stop the sweep
                failed += 1
                log.warning("could not remove the repo of task %s: %s", task_id, exc)
            else:
                removed.append(task_id)
        if removed:
            log.info("removed the repos of %d tasks that ended a week ago or more", len(removed))
        return acts.JanitorResult(
            removed=removed, kept=len(found) - len(removed) - failed, failed=failed
        )
