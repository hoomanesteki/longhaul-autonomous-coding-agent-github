"""The poller's activity: poll_once, which starts a task for each newly labeled issue (LH-307).

PollerWorkflow calls it once per tick of the Temporal Schedule (ADR-0008). One poll goes through
the repos of the allowlist in config order. For each repo it lists the open issues that carry the
poller label, and for each issue that has no task yet it builds the TaskSpec (github/intake.py)
and starts a TaskWorkflow with durable/client.start_task. The task id is the workflow id, so the
poll can run any number of times and an issue still gets one task.

What the poller leaves to the task. It only reads from GitHub. The "Picked up" comment and the
`longhaul:working` label are the task's own side effects, written by its first activity with an
idempotency key (LH-306). A poller that wrote them would do it before the task exists, and a
second poller could write them again.

Every task the poller starts plans first (LH-501): the planner writes the plan, and the task
posts it on the issue, before any code is written.

An issue already has its task when any of these holds, and the poll leaves it alone:

* the `tasks` table has a row with the task id, whatever its status. A finished task keeps its
  row, so a label that stays on the issue never runs the task again. Temporal alone would not
  keep that promise: it forgets a closed workflow when its retention runs out (db/tasks.py).
* the workflow exists. start_task rejects a duplicate id while the run is open and after it
  closed (REJECT_DUPLICATE), and the poll counts WorkflowAlreadyStartedError as done. That is
  the case of a poll that died between the start and the row, and the row is written then.

The workflow is started first and the row after it, so a poll that dies in between loses
nothing: the next poll finds the workflow, writes the row and moves on.

One repo that fails does not stop the others. A repo that GitHub cannot read (a typo in the
config, a token without access, a 5xx) is logged, the poll goes on with the next repo, and the
activity fails at the end, so the Temporal UI shows a failed poll for as long as a repo is
broken. The failure is retryable when any repo's was, after the longest wait GitHub asked for, cut
to the longest backoff of the policy (see _retry_delay). The retry repeats the work of the
repos that went well at the cost of one list call each: their issues now have rows.

Unknown means no, as in intake.py. The comments of a new issue are filtered by the permission of
their authors, and a permission that GitHub refuses to tell (a 403 or 404 for that login) counts
as none. It must not keep the issue from becoming a task, and it cannot let a stranger's comment
into the prompt.

Settings that cannot work stop the poll for good, with the error type PollerConfigError: no
GitHub token on the worker, or an allowlist that intake.py refuses (the same repo twice, a name
that is not owner/name).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from longhaul.contracts import TaskSpec
from longhaul.db.tasks import TaskStore
from longhaul.durable import acts
from longhaul.durable.activities.support import heartbeating
from longhaul.durable.client import start_task
from longhaul.github.client import GitHubClient, GitHubError, GitHubFatal, GitHubRetryable
from longhaul.github.intake import is_allowlisted, is_local, issue_to_spec, task_id_for
from longhaul.settings import AppConfig, RepoEntry

log = logging.getLogger(__name__)

# The failure message ends up in the workflow history and in the UI, so it stays short.
MAX_MESSAGE_CHARS = 500


@dataclass
class _Tally:
    repos: int = 0
    issues: int = 0
    started: int = 0
    known: int = 0
    failures: list[tuple[str, Exception]] = field(default_factory=list)

    def result(self) -> acts.PollResult:
        return acts.PollResult(
            repos=self.repos, issues=self.issues, started=self.started, known=self.known
        )


def _config_error(message: str) -> ApplicationError:
    return ApplicationError(message, type=acts.POLLER_CONFIG, non_retryable=True)


def _retry_delay(failures: Sequence[tuple[str, Exception]]) -> timedelta | None:
    """The longest wait that GitHub asked for, but never more than the policy's own backoff.

    A spent primary quota can ask for an hour. The run of a poll ends after POLLER_RUN_TIMEOUT,
    and the Schedule skips its ticks until then, so a longer wait would end the run by timeout
    with no GitHub message on it. At the cap the attempts run out and the run fails with the
    message instead, and the next tick polls again.
    """
    waits = [
        exc.retry_after_s
        for _, exc in failures
        if isinstance(exc, GitHubRetryable) and exc.retry_after_s is not None
    ]
    if not waits:
        return None
    delay = timedelta(seconds=max(waits))
    longest = acts.POLL_ONCE.retry.maximum_interval
    return delay if longest is None else min(delay, longest)


def _failed(failures: Sequence[tuple[str, Exception]]) -> ApplicationError:
    """The error that ends a poll in which at least one repo failed.

    It is retryable when any of the repos failed in a way that a later try may fix. A mix of
    that and a failure for good is retried too, because the retry is what finishes the repos
    that can still be read.
    """
    detail = "; ".join(f"{repo}: {exc}" for repo, exc in failures)
    if len(detail) > MAX_MESSAGE_CHARS:
        detail = detail[:MAX_MESSAGE_CHARS] + "..."
    message = f"polling failed for {len(failures)} repo(s): {detail}"
    if any(isinstance(exc, GitHubRetryable) for _, exc in failures):
        return ApplicationError(
            message, type=acts.GITHUB_RETRYABLE, next_retry_delay=_retry_delay(failures)
        )
    return ApplicationError(message, type=acts.GITHUB_FATAL, non_retryable=True)


def _task_id(repo: str, issue: Mapping[str, Any]) -> str:
    number: Any = issue.get("number")  # task_id_for raises ValueError when it is not usable
    return task_id_for(repo, number)


def _logins(comments: Sequence[Mapping[str, Any]]) -> list[str]:
    """The distinct authors of the comments, in lower case, in the order they first appear."""
    found: dict[str, None] = {}
    for comment in comments:
        user = comment.get("user")  # null for an account that was deleted
        login = user.get("login") if isinstance(user, Mapping) else None
        if isinstance(login, str) and login:
            found.setdefault(login.lower())
    return list(found)


class PollerActivities:
    """poll_once, with the GitHub client, the task tables and the allowlist it works from.

    `github` is None on a worker that has no token. The activity is still registered, and a poll
    on such a worker fails with a message that names the setting. `config` is read once, when
    the worker starts, as the rest of the worker's settings are. `task_queue` is the queue that
    the tasks start on, which is the queue of the worker itself.
    """

    def __init__(
        self,
        github: GitHubClient | None,
        store: TaskStore,
        config: AppConfig,
        *,
        task_queue: str,
    ) -> None:
        self._github = github
        self._store = store
        self._config = config
        self._task_queue = task_queue

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.poll_once]

    @activity.defn(name=acts.POLL_ONCE.name)
    async def poll_once(self) -> acts.PollResult:
        # The worker's own Temporal client starts the tasks. It carries the tracing interceptor
        # and the pydantic converter, and it needs no second connection.
        async with heartbeating():
            return await self.poll(activity.client())

    async def poll(self, client: Client) -> acts.PollResult:
        """One pass over the allowlist. See the module docstring for what it promises."""
        github = self._github
        if github is None:
            raise _config_error(
                "the worker has no GitHub token, so it cannot poll. Set LONGHAUL_GITHUB_TOKEN"
            )
        try:
            await self._store.sync_repos(self._config)
            entries = self._allowlisted()
        except ValueError as exc:
            raise _config_error(f"the allowlist cannot be used: {exc}") from None
        tally = _Tally()
        for entry in entries:
            tally.repos += 1
            try:
                await self._poll_repo(client, github, entry, tally)
            except (GitHubError, ValueError) as exc:
                # ValueError is an issue that GitHub sent without a usable number or title.
                # Waiting does not change it, so it counts as a failure for good.
                failure = exc if isinstance(exc, GitHubError) else GitHubFatal(str(exc))
                log.warning("polling %s failed: %s", entry.repo, failure)
                tally.failures.append((entry.repo, failure))
        if tally.failures:
            raise _failed(tally.failures)
        log.info(
            "polled %d repos: %d labeled issues, %d tasks started, %d already known",
            tally.repos,
            tally.issues,
            tally.started,
            tally.known,
        )
        return tally.result()

    def _allowlisted(self) -> list[RepoEntry]:
        # A local repo has no issues. is_allowlisted refuses a config that lists a repo twice.
        return [
            entry
            for entry in self._config.repos
            if not is_local(entry.repo) and is_allowlisted(entry.repo, self._config)
        ]

    async def _poll_repo(
        self, client: Client, github: GitHubClient, entry: RepoEntry, tally: _Tally
    ) -> None:
        issues = await github.list_labeled_issues(entry.repo, self._config.poller.label)
        tally.issues += len(issues)
        ids = [_task_id(entry.repo, issue) for issue in issues]
        known = await self._store.known(ids)
        for issue, task_id in zip(issues, ids, strict=True):
            if task_id in known:
                tally.known += 1
                continue
            spec = await self._spec(github, entry, issue)
            if spec is None:
                continue  # issue_to_spec has logged why
            if await self._start(client, spec):
                tally.started += 1
            else:
                tally.known += 1
            await self._store.record_pickup(spec)

    async def _spec(
        self, github: GitHubClient, entry: RepoEntry, issue: Mapping[str, Any]
    ) -> TaskSpec | None:
        comments = await github.list_comments(entry.repo, issue["number"])
        permissions: dict[str, Mapping[str, Any]] = {}
        for login in _logins(comments):
            try:
                permissions[login] = await github.get_collaborator_permission(entry.repo, login)
            except GitHubFatal as exc:
                log.warning(
                    "cannot read the permission of %s on %s, so their comments are left out: %s",
                    login,
                    entry.repo,
                    exc,
                )
        return issue_to_spec(entry, issue, comments, permissions)

    async def _start(self, client: Client, spec: TaskSpec) -> bool:
        """Start the workflow of the task. False when it exists already, which is as good."""
        try:
            # A task of the poller plans first: the plan is what a maintainer reads before any
            # code is written (LH-501).
            await start_task(client, spec, task_queue=self._task_queue, plan=True)
        except WorkflowAlreadyStartedError:
            log.info("task %s exists already, so it is not started again", spec.task_id)
            return False
        log.info("started task %s for issue #%s of %s", spec.task_id, spec.issue_number, spec.repo)
        return True
