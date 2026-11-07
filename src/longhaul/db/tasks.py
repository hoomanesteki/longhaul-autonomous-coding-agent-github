"""The two tables the poller works with: `repo_settings` and `tasks` (LH-307, LH-308).

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

The comment cursor (LH-308). The poller reads the comments of each open task issue and acts on the
commands in them. `last_comment_at` and `last_comment_id` mark the newest comment it has handled,
and a poll asks GitHub only for what came after. The time is for the `since` of the request. The
id is what decides, because GitHub stamps a comment to the second and an id only grows. A cursor
moves forward only (`advance_comments`), and moves after the signal of a command was sent, so a
poll that dies in between sends the command again and a poll that is retried never sends one twice.
A row that was written before the columns existed has no cursor, and its comments are read from
the time the task started.

An issue can have more than one task. `/longhaul retry` starts the next attempt after the first
has ended, with the id `<task id>-a2`, `-a3` and so on (github/intake.py). `latest_attempts` finds
the newest attempt of each issue, which is the one that commands are meant for.

A row stays after its task ends, and that is the point of `known`. Temporal forgets a closed
workflow when its retention runs out (one day on the dev server), and then a start with the same
id would run a finished task again for as long as the label stays on the issue (ADR-0004,
appendix section 6). The row is what keeps it from happening.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, Self

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from longhaul.contracts import Phase, TaskSpec
from longhaul.db.models import Task
from longhaul.github.intake import attempt_of, sync_repo_settings
from longhaul.settings import AppConfig

__all__ = [
    "ACTIVE",
    "CommentCursor",
    "MemoryTaskStore",
    "SqlTaskStore",
    "TaskRow",
    "TaskStore",
]

# The status of a task that the poller has started. Later stories write the ones that follow.
ACTIVE = "active"


@dataclass(frozen=True)
class CommentCursor:
    """The newest issue comment that has been handled: when it was written, and its id.

    Comments are handled in the order of their ids, so a comment is new when its id is higher.
    """

    at: datetime
    comment_id: int


class TaskStore(Protocol):
    async def sync_repos(self, config: AppConfig) -> None:
        """Make the `repo_settings` table match the allowlist in the config (LH-303)."""
        ...

    async def known(self, task_ids: Sequence[str]) -> set[str]:
        """The ids among `task_ids` that have a row, whatever its status."""
        ...

    async def record_pickup(self, spec: TaskSpec, *, cursor: CommentCursor | None = None) -> None:
        """Write the row of a task that has started, or update the title and settings of it.

        `cursor` is where the comments of the task begin. It is stored with a new row and
        ignored for a row that exists, so a poll that meets a task again cannot move it.
        """
        ...

    async def latest_attempts(
        self, repo: str, issue_numbers: Sequence[int]
    ) -> dict[int, "TaskRow"]:
        """The newest attempt of each issue in `issue_numbers` that has a task, by issue number."""
        ...

    async def advance_comments(self, task_id: str, cursor: CommentCursor) -> None:
        """Move the cursor of a task forward. A cursor that is not ahead of the stored one, and a
        task without a row, change nothing."""
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
    last_comment_at: datetime | None = None
    last_comment_id: int | None = None

    @property
    def cursor(self) -> CommentCursor | None:
        if self.last_comment_at is None or self.last_comment_id is None:
            return None
        return CommentCursor(self.last_comment_at, self.last_comment_id)


def _row(spec: TaskSpec, started_at: datetime, cursor: CommentCursor | None = None) -> TaskRow:
    return TaskRow(
        id=spec.task_id,
        repo=spec.repo,
        issue_number=spec.issue_number,
        title=spec.title,
        status=ACTIVE,
        phase=Phase.QUEUED.value,
        settings=spec.settings.model_dump(mode="json"),
        started_at=started_at,
        last_comment_at=cursor.at if cursor else None,
        last_comment_id=cursor.comment_id if cursor else None,
    )


def _newest_attempts(rows: Iterable[TaskRow]) -> dict[int, TaskRow]:
    """The row with the highest attempt for each issue number."""
    newest: dict[int, TaskRow] = {}
    for row in rows:
        number = row.issue_number
        if number is not None and (
            number not in newest or attempt_of(row.id) > attempt_of(newest[number].id)
        ):
            newest[number] = row
    return newest


def _is_ahead(cursor: CommentCursor, stored: int | None) -> bool:
    return stored is None or cursor.comment_id > stored


class MemoryTaskStore:
    """The rows in memory, for tests. `rows` and `synced` are public for a test to read."""

    def __init__(self) -> None:
        self.rows: dict[str, TaskRow] = {}
        self.synced: list[AppConfig] = []

    async def sync_repos(self, config: AppConfig) -> None:
        self.synced.append(config)

    async def known(self, task_ids: Sequence[str]) -> set[str]:
        return {task_id for task_id in task_ids if task_id in self.rows}

    async def record_pickup(self, spec: TaskSpec, *, cursor: CommentCursor | None = None) -> None:
        row = _row(spec, datetime.now(UTC), cursor)
        kept = self.rows.get(spec.task_id)
        if kept is None:
            self.rows[spec.task_id] = row
        else:
            kept.title, kept.settings = row.title, row.settings

    async def latest_attempts(self, repo: str, issue_numbers: Sequence[int]) -> dict[int, TaskRow]:
        wanted = set(issue_numbers)
        found = (
            row for row in self.rows.values() if row.repo == repo and row.issue_number in wanted
        )
        return _newest_attempts(found)

    async def advance_comments(self, task_id: str, cursor: CommentCursor) -> None:
        row = self.rows.get(task_id)
        if row is not None and _is_ahead(cursor, row.last_comment_id):
            row.last_comment_at, row.last_comment_id = cursor.at, cursor.comment_id


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

    async def record_pickup(self, spec: TaskSpec, *, cursor: CommentCursor | None = None) -> None:
        row = _row(spec, datetime.now(UTC), cursor)
        insert_row = insert(Task).values(
            id=row.id,
            repo=row.repo,
            issue_number=row.issue_number,
            title=row.title,
            status=row.status,
            phase=row.phase,
            settings=row.settings,
            started_at=row.started_at,
            last_comment_at=row.last_comment_at,
            last_comment_id=row.last_comment_id,
        )
        # Only what comes from the issue is updated. The status and the phase belong to the task,
        # and the cursor to the poll that handles its comments.
        upsert = insert_row.on_conflict_do_update(
            index_elements=[Task.id],
            set_={"title": insert_row.excluded.title, "settings": insert_row.excluded.settings},
        )
        async with self._sessions.begin() as session:
            await session.execute(upsert)

    async def latest_attempts(self, repo: str, issue_numbers: Sequence[int]) -> dict[int, TaskRow]:
        if not issue_numbers:
            return {}
        query = select(Task).where(Task.repo == repo, Task.issue_number.in_(list(issue_numbers)))
        async with self._sessions() as session:
            found = (await session.execute(query)).scalars().all()
        return _newest_attempts(_to_row(task) for task in found)

    async def advance_comments(self, task_id: str, cursor: CommentCursor) -> None:
        # The comparison is in the statement, so two polls that race cannot move it back.
        move = (
            update(Task)
            .where(Task.id == task_id)
            .where(or_(Task.last_comment_id.is_(None), Task.last_comment_id < cursor.comment_id))
            .values(last_comment_at=cursor.at, last_comment_id=cursor.comment_id)
        )
        async with self._sessions.begin() as session:
            await session.execute(move)


def _to_row(task: Task) -> TaskRow:
    return TaskRow(
        id=task.id,
        repo=task.repo,
        issue_number=task.issue_number,
        title=task.title,
        status=task.status,
        phase=task.phase,
        settings=task.settings,
        # The poller always writes it. created_at is there for a row that something else wrote.
        started_at=task.started_at or task.created_at,
        last_comment_at=task.last_comment_at,
        last_comment_id=task.last_comment_id,
    )
