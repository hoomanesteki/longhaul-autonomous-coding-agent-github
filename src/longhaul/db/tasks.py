"""The two tables the poller works with: `repo_settings` and `tasks` (LH-307).

`TaskStore` is the small interface that the poller activity uses, so a unit test can run it
without Postgres. `SqlTaskStore` writes the tables and `MemoryTaskStore` keeps the same rows in
memory, like `Recorder` and `MemoryRecorder` in db/records.py. tests/integration/test_poller.py
runs the poller on the SQL store.

What the poller writes to `tasks`, and why it is only this:

* One row when a task is picked up, with the repo, the issue, the title, the settings the task
  runs with, the status `active` and the phase `queued`. The workflow starts in that phase.
* Nothing after that. The status and the phase follow the task through later stories (the
  outcome, the cost, the PR url). A row that exists is never put back to `queued`: a poll that
  meets a row again updates only the title and the settings, which come from the issue.

The poller starts the workflow first and writes the row after it. The other order would lose a
task when the poller dies in between: the row would say the task is known, the next poll would
skip it, and no workflow would exist. In this order the worst case is a workflow with no row,
and the next poll finds the workflow (WorkflowAlreadyStartedError) and writes the row.

A row stays after its task ends, and that is the point of `known`. Temporal forgets a closed
workflow when its retention runs out (one day on the dev server), and then a start with the same
id would run a finished task again for as long as the label stays on the issue (ADR-0004,
appendix section 6). The row is what keeps it from happening.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, Self

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from longhaul.contracts import Phase, TaskSpec
from longhaul.db.models import Task
from longhaul.github.intake import sync_repo_settings
from longhaul.settings import AppConfig

__all__ = ["ACTIVE", "MemoryTaskStore", "SqlTaskStore", "TaskRow", "TaskStore"]

# The status of a task that the poller has started. Later stories write the ones that follow.
ACTIVE = "active"


class TaskStore(Protocol):
    async def sync_repos(self, config: AppConfig) -> None:
        """Make the `repo_settings` table match the allowlist in the config (LH-303)."""
        ...

    async def known(self, task_ids: Sequence[str]) -> set[str]:
        """The ids among `task_ids` that have a row, whatever its status."""
        ...

    async def record_pickup(self, spec: TaskSpec) -> None:
        """Write the row of a task that has started, or update the title and settings of it."""
        ...


@dataclass
class TaskRow:
    """A row of `tasks`, as far as the poller writes it."""

    id: str
    repo: str
    issue_number: int | None
    title: str
    status: str
    phase: str
    settings: dict[str, Any]
    started_at: datetime


def _row(spec: TaskSpec, started_at: datetime) -> TaskRow:
    return TaskRow(
        id=spec.task_id,
        repo=spec.repo,
        issue_number=spec.issue_number,
        title=spec.title,
        status=ACTIVE,
        phase=Phase.QUEUED.value,
        settings=spec.settings.model_dump(mode="json"),
        started_at=started_at,
    )


class MemoryTaskStore:
    """The rows in memory, for tests. `rows` and `synced` are public for a test to read."""

    def __init__(self) -> None:
        self.rows: dict[str, TaskRow] = {}
        self.synced: list[AppConfig] = []

    async def sync_repos(self, config: AppConfig) -> None:
        self.synced.append(config)

    async def known(self, task_ids: Sequence[str]) -> set[str]:
        return {task_id for task_id in task_ids if task_id in self.rows}

    async def record_pickup(self, spec: TaskSpec) -> None:
        row = _row(spec, datetime.now(UTC))
        kept = self.rows.get(spec.task_id)
        if kept is None:
            self.rows[spec.task_id] = row
        else:
            kept.title, kept.settings = row.title, row.settings


class SqlTaskStore:
    """The tables in Postgres. Every call uses a session of its own and commits it."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: AsyncEngine) -> Self:
        return cls(async_sessionmaker(engine, expire_on_commit=False))

    async def sync_repos(self, config: AppConfig) -> None:
        async with self._sessions() as session:
            await sync_repo_settings(config, session)

    async def known(self, task_ids: Sequence[str]) -> set[str]:
        if not task_ids:
            return set()
        async with self._sessions() as session:
            found = await session.execute(select(Task.id).where(Task.id.in_(list(task_ids))))
            return set(found.scalars())

    async def record_pickup(self, spec: TaskSpec) -> None:
        row = _row(spec, datetime.now(UTC))
        insert_row = insert(Task).values(
            id=row.id,
            repo=row.repo,
            issue_number=row.issue_number,
            title=row.title,
            status=row.status,
            phase=row.phase,
            settings=row.settings,
            started_at=row.started_at,
        )
        # Only what comes from the issue is updated. The status and the phase belong to the task.
        upsert = insert_row.on_conflict_do_update(
            index_elements=[Task.id],
            set_={"title": insert_row.excluded.title, "settings": insert_row.excluded.settings},
        )
        async with self._sessions.begin() as session:
            await session.execute(upsert)
