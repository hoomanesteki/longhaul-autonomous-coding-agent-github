"""The commands in the comments of a task's issue, as poll_once carries them out (LH-308).

A maintainer steers a task with a comment such as `/longhaul hint look at add()` (github/commands.py
lists them). After the poller has started the new tasks, it hands each open labeled issue that has a
task to this module, which does four things:

1. **Reads the new comments.** It asks GitHub for the comments since the cursor of the task
   (db/tasks.py) and keeps those with a higher id than the newest one it has handled. Comments
   that were written before the task was picked up are not commands. A cursor is stored with the
   pickup that stands for the comments the spec was built from.
2. **Drops what is not a command.** A comment of ours (it carries the hidden marker of
   side_effects.py), a comment of a bot, and a comment that does not begin with a command.
3. **Checks who wrote it.** The permission comes from the collaborator permission endpoint, on
   every poll that meets a command from that user, and a permission that GitHub refuses to tell
   counts as none (unknown means no, as in intake.py). Someone without write access gets one
   polite reply for each task, through `post_comment` with a key that holds their login, so a
   retried poll and a second command post nothing more. The command is not carried out.
4. **Carries the command out.** Every command but `retry` is a signal to the latest attempt of
   the task: `approve_plan`, `reject_plan`, `hint`, `add_budget`, `cancel` and `report_status`.
   `retry` starts the next attempt when the latest one has ended (see below).

A command is sent once. The cursor moves forward after its signal went out, and the poll does it
comment by comment, so a poll that fails halfway and is retried by Temporal sends the rest and
nothing it has sent. The window that remains is a worker that dies between the signal and the
write of the cursor, which sends that one command again. A repeated `approve`, `reject` or
`cancel` changes nothing, since the first answer wins. A repeated `status` posts another status
comment, a repeated `hint` shows the model the hint twice, and a repeated `budget` adds twice,
which the next status shows and a person can correct.

A task that has ended cannot be signalled. Temporal answers a signal to a closed workflow with
NOT_FOUND, and the command is dropped with a log line. It is not kept for later, since a command
written for a task that has gone is no order for the next one. `retry` is the exception.

`retry` on a task that is still running does nothing. On one that has ended, the poller starts
`<task id>-a2` (`-a3` and so on, github/intake.py) from the issue as it is now, writes its row with
the cursor at the `retry` comment, and moves the cursor of the old attempt past it. The workflow is
started first and the row after it, as for a new task, so a poll that dies in between finds the
workflow on the next try (WorkflowAlreadyStartedError) and writes the row. Commands that follow the
`retry` in the same poll, and in every later one, go to the new attempt.

The poller still never writes the labels or the pickup comment: they belong to the task. The reply
to a stranger is the one comment the poll posts, because no task owns that answer, and it is
idempotent like any other write.

An issue that fails does not stop the others. Its error is returned with the name of the issue, and
the poll fails after the last issue, as it does for a repo that cannot be read.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from longhaul.db.tasks import CommentCursor, TaskRow, TaskStore
from longhaul.durable.client import get_task
from longhaul.github import lifecycle
from longhaul.github.client import GitHubClient, GitHubError, GitHubFatal
from longhaul.github.commands import Command, CommandKind, may_steer, parse_command
from longhaul.github.intake import attempt_of
from longhaul.github.side_effects import Ledger, has_marker, post_comment
from longhaul.settings import RepoEntry

__all__ = [
    "SIGNALS",
    "BeginAttempt",
    "Steered",
    "Steering",
    "pickup_cursor",
    "read_time",
]

log = logging.getLogger(__name__)

# The signal of TaskWorkflow that each command sends. `retry` sends none.
SIGNALS: dict[CommandKind, str] = {
    CommandKind.APPROVE: "approve_plan",
    CommandKind.REJECT: "reject_plan",
    CommandKind.HINT: "hint",
    CommandKind.BUDGET: "add_budget",
    CommandKind.CANCEL: "cancel",
    CommandKind.STATUS: "report_status",
}

# GitHub documents `since` as "updated after", and the client as "at or after". The cursor is
# the time of a comment that has been handled, so a request one second back loses nothing if the
# first is the truth, and costs one comment that the id check drops if the second is.
SINCE_SLACK = timedelta(seconds=1)

# Starts the attempt of an issue and writes its row. It returns the task id, or None when intake
# declines the issue. The arguments are the client, the repo, the issue, the attempt and the
# cursor of the row.
BeginAttempt = Callable[
    [Client, RepoEntry, Mapping[str, Any], int, CommentCursor], Awaitable[str | None]
]


def read_time(text: object) -> datetime:
    """A time from GitHub, such as `2026-03-01T12:00:00Z`, as an aware UTC datetime.

    Raises ValueError for anything else, so a changed API does not pass for the year 1.
    """
    if not isinstance(text, str):
        raise ValueError(f"expected a time written as text, got {text!r}")
    moment = datetime.fromisoformat(text)
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@dataclass(frozen=True)
class _Comment:
    id: int
    at: datetime
    login: str
    is_bot: bool
    body: str

    @property
    def cursor(self) -> CommentCursor:
        return CommentCursor(self.at, self.id)


def _read_comment(raw: Mapping[str, Any]) -> _Comment | None:
    """A comment as far as steering needs it, or None for one without an id or a time."""
    comment_id = raw.get("id")
    if isinstance(comment_id, bool) or not isinstance(comment_id, int):
        return None
    try:
        at = read_time(raw.get("created_at"))
    except ValueError:
        return None
    user = raw.get("user")  # null for an account that was deleted
    login = user.get("login") if isinstance(user, Mapping) else None
    kind = user.get("type") if isinstance(user, Mapping) else None
    body = raw.get("body")
    return _Comment(
        id=comment_id,
        at=at,
        login=login if isinstance(login, str) else "",
        is_bot=kind == "Bot" or (isinstance(login, str) and login.endswith("[bot]")),
        body=body if isinstance(body, str) else "",
    )


def pickup_cursor(issue: Mapping[str, Any], comments: Sequence[Mapping[str, Any]]) -> CommentCursor:
    """Where the comments of a new task begin: the newest comment the spec was built from.

    Everything older is part of the spec or of the talk before the label, and none of it is a
    command. An issue with no comment begins at its own creation, with id 0, so every comment
    that follows counts.
    """
    seen = [c for c in map(_read_comment, comments) if c is not None]
    if seen:
        return max(seen, key=lambda c: c.id).cursor
    return CommentCursor(read_time(issue.get("created_at")), 0)


@dataclass
class Steered:
    """What steering one repo came to: the commands carried out, and the issues that failed."""

    commands: int = 0
    failures: list[tuple[str, Exception]] = field(default_factory=list)


class Steering:
    """Reads the comments of the open task issues of a repo and carries out their commands.

    One instance serves one poll. It remembers who may steer for that poll only, so a person
    whose role changed is judged again by the next poll.
    """

    def __init__(
        self, github: GitHubClient, store: TaskStore, ledger: Ledger, begin: BeginAttempt
    ) -> None:
        self._github = github
        self._store = store
        self._ledger = ledger
        self._begin = begin
        self._allowed: dict[tuple[str, str], bool] = {}

    async def steer_repo(
        self, client: Client, entry: RepoEntry, issues: Sequence[Mapping[str, Any]]
    ) -> Steered:
        """Handle the new comments of each issue in `issues` that has a task."""
        numbers = [n for n in (issue.get("number") for issue in issues) if isinstance(n, int)]
        latest = await self._store.latest_attempts(entry.repo, numbers)
        steered = Steered()
        for issue in issues:
            number = issue.get("number")
            row = latest.get(number) if isinstance(number, int) else None
            if row is None:
                continue  # no task, because intake declined the issue
            try:
                steered.commands += await self._steer_issue(client, entry, issue, row)
            except (GitHubError, ValueError) as exc:
                # ValueError is a side effect that cannot be written, or a comment that GitHub
                # sent in a shape we do not read. Waiting does not change either.
                where = f"{entry.repo}#{number}"
                log.warning("steering %s failed: %s", where, exc)
                steered.failures.append((where, exc))
        return steered

    async def _steer_issue(
        self, client: Client, entry: RepoEntry, issue: Mapping[str, Any], row: TaskRow
    ) -> int:
        cursor = row.cursor or CommentCursor(row.started_at, 0)
        listed = await self._github.list_comments_since(
            entry.repo, issue["number"], cursor.at - SINCE_SLACK
        )
        fresh = sorted(
            (c for c in map(_read_comment, listed) if c is not None and c.id > cursor.comment_id),
            key=lambda c: c.id,
        )
        target, carried_out = row.id, 0
        handled: CommentCursor | None = None
        for comment in fresh:
            command = self._command_of(comment)
            if command is None:
                continue
            target, sent = await self._carry_out(client, entry, issue, target, comment, command)
            carried_out += sent
            # After the signal, and before the next comment: see the module docstring.
            await self._store.advance_comments(target, comment.cursor)
            handled = comment.cursor
        newest = fresh[-1].cursor if fresh else None
        if newest is not None and newest != handled:
            await self._store.advance_comments(target, newest)
        return carried_out

    @staticmethod
    def _command_of(comment: _Comment) -> Command | None:
        if comment.is_bot or not comment.login or has_marker(comment.body):
            return None
        return parse_command(comment.body)

    async def _carry_out(
        self,
        client: Client,
        entry: RepoEntry,
        issue: Mapping[str, Any],
        target: str,
        comment: _Comment,
        command: Command,
    ) -> tuple[str, int]:
        """Do what the command asks. Returns the task the next command is for, and 1 if it ran."""
        if not await self._may_steer(entry.repo, comment.login):
            reply = lifecycle.denied_comment(target, entry.repo, issue["number"], comment.login)
            await post_comment(self._github, self._ledger, reply)
            log.info(
                "%s may not steer %s, so their %s was answered", comment.login, target, command.kind
            )
            return target, 0
        if command.kind is CommandKind.RETRY:
            return await self._retry(client, entry, issue, target, comment)
        return target, int(await self._send(client, target, command, comment.login))

    async def _may_steer(self, repo: str, login: str) -> bool:
        key = (repo, login.lower())
        if key not in self._allowed:
            try:
                answer = await self._github.get_collaborator_permission(repo, login)
            except GitHubFatal as exc:
                # A 403 or 404 for this login. It must not stop the poll, and it cannot let
                # the command through.
                log.warning("cannot read the permission of %s on %s: %s", login, repo, exc)
                self._allowed[key] = False
            else:
                self._allowed[key] = may_steer(answer)
        return self._allowed[key]

    async def _send(self, client: Client, task_id: str, command: Command, login: str) -> bool:
        """Send the signal of a command. False when the task has ended and cannot take it."""
        name, args = SIGNALS[command.kind], _arguments(command, login)
        try:
            await get_task(client, task_id).signal(name, args=args)
        except RPCError as exc:
            if exc.status is not RPCStatusCode.NOT_FOUND:
                raise
            log.info("task %s has ended, so the %s of %s is dropped", task_id, command.kind, login)
            return False
        log.info("sent %s to task %s for %s", name, task_id, login)
        return True

    async def _retry(
        self,
        client: Client,
        entry: RepoEntry,
        issue: Mapping[str, Any],
        target: str,
        comment: _Comment,
    ) -> tuple[str, int]:
        if await self._is_running(client, target):
            log.info("retry from %s ignored: task %s is still running", comment.login, target)
            return target, 0
        started = await self._begin(client, entry, issue, attempt_of(target) + 1, comment.cursor)
        if started is None:
            return target, 0
        # The new row begins at this comment already. The old one must not read it again.
        await self._store.advance_comments(target, comment.cursor)
        return started, 1

    @staticmethod
    async def _is_running(client: Client, task_id: str) -> bool:
        """True while the workflow is open. One the server has forgotten counts as ended."""
        try:
            described = await get_task(client, task_id).describe()
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                return False
            raise
        return described.status is WorkflowExecutionStatus.RUNNING


def _arguments(command: Command, login: str) -> list[Any]:
    """The arguments of the signal for a command, after the name of the person who sent it."""
    match command.kind:
        case CommandKind.REJECT | CommandKind.HINT | CommandKind.CANCEL:
            return [login, command.text]
        case CommandKind.BUDGET:
            return [login, command.usd, command.turns]
        case _:
            return [login]
