"""The build_pr_branch and deliver activities: a finished task becomes a draft PR (LH-306).

The plan puts these in durable/activities/github.py. That module holds the poller (LH-307), so
the delivery has a file of its own.

`build_pr_branch` runs on the host, from the task's bare repo. It reads the default branch from
the API, titles the commits, and makes the branch `longhaul-pr/<n>` on top of the base
(checkpoints/squash.py). A task without a plan gets one commit, titled with the summary the agent
gave to `finish`. A task with a plan (LH-503) gets one commit for each step that is done, titled
from the step's description, and one for the work after the last step, titled from the summary.
The workflow hands over the plan with the status of each step, and the checkpoints that the steps
left are read from the `checkpoints` table, because the sha of a checkpoint is not in the state.
The pull request keeps the title of the summary either way. The base is the sha that the workflow
handed over, not the `base` tag of the repo, which the sandbox can move. It pushes nothing.
The branch is the same commit on every attempt, so a retry changes nothing.

`deliver` takes that branch to GitHub in two steps, both through the idempotency ledger of
github/side_effects.py and both safe to run again:

1. **Push.** push_branch refuses the default branch and the protected names, never forces, and
   signs in with the token on the command line of that one git process, so the sandbox never sees
   it (ADR-0009). The push has a ledger row of its own (`push`, key `<task>:push:1`), which keeps
   the audit trail whole and lets a retry skip the push once it has landed.
2. **Pull request.** `create_pr`, the plain function and not the activity, opens a draft with key
   `<task>:pr:1`. It returns the URL of an open pull request from the same branch instead of
   opening a second one, even when the ledger was lost. The request is a draft until a verifier
   has passed the work (Phase 5).

The body is built here, and not in the workflow, from what only the worker can read: the diff of
the branch, and the `tool_calls` row and output blob of the last `run_tests` call. It is the same
bytes on every attempt (github/pr_body.py), because `create_pr` refuses a reused key with other
text. Everything it needs is fixed in the input of the activity or in rows that no longer change
once the task has finished.

Failures. A GitHub error is retryable or not as the client classified it (support.github_failure).
A request that no retry can fix, such as a key used for another effect, a bad name or a template
that is missing, is not retried. A push to the default branch (failure mode F15) is not retried
either, and is recorded as a `branch_refused` event. A push that git rejects, for example because
the remote branch moved, raises CheckpointError and is retried by the policy of `deliver`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from jinja2 import TemplateError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from longhaul.blobs.base import BlobError, BlobStoreBase
from longhaul.checkpoints.git_store import CheckpointError, GitCheckpointStore, RepoNotFoundError
from longhaul.checkpoints.push import ForbiddenBranchError, push_branch
from longhaul.checkpoints.squash import (
    Group,
    NothingToDeliverError,
    StepEnd,
    build_pr_branch,
    commit_subject,
    file_stats,
    group_by_step,
    parse_author,
    pr_branch_name,
    repo_scope,
)
from longhaul.db.records import CheckpointRecord, ToolCallRecord
from longhaul.durable import acts
from longhaul.durable.activities.support import github_failure, heartbeating, not_retried, small
from longhaul.github.client import GitHubClient, GitHubError
from longhaul.github.inputs import PullRequestInput
from longhaul.github.pr_body import LastTestRun, render_pr_body
from longhaul.github.remote import GitHubRemote
from longhaul.github.side_effects import (
    Ledger,
    SideEffectError,
    Wanted,
    create_pr,
    effect_key,
    run_once,
)
from longhaul.tools.exec import parse_pytest_summary

log = logging.getLogger(__name__)

# The event type that a refused push records. Plan section 3.5 lists the first types, and later
# stories add more.
BRANCH_REFUSED = "branch_refused"
# The description of a commit when the summary of the agent has no words in it.
FALLBACK_DESCRIPTION = "resolve the issue"
TESTS_TOOL = "run_tests"


class DeliveryRecords(Protocol):
    """The part of the recorder that these activities use: one event written, rows read."""

    async def record_event(self, task_id: str, type: str, payload: dict[str, Any]) -> None: ...

    async def last_tool_call(self, task_id: str, name: str) -> ToolCallRecord | None: ...

    async def step_checkpoints(self, task_id: str) -> list[CheckpointRecord]: ...


class DeliveryActivities:
    """build_pr_branch and deliver, with what they need.

    `store` holds the bare repos, `remote` is how the host reaches GitHub for git and for the
    default branch, `client` makes the pull request, and `ledger` records the push and the pull
    request. `records` is read for the last test run and written for a refused push, and `blobs`
    holds the output of that test run. `author` is `Name <address>` for the commits, and a value
    that git would misread stops the worker when it starts. `templates` is the folder that holds
    pr_body.md.j2.
    """

    def __init__(
        self,
        store: GitCheckpointStore,
        remote: GitHubRemote,
        client: GitHubClient,
        ledger: Ledger,
        records: DeliveryRecords,
        blobs: BlobStoreBase,
        *,
        author: str,
        templates: Path,
    ) -> None:
        parse_author(author)
        self._store = store
        self._remote = remote
        self._client = client
        self._ledger = ledger
        self._records = records
        self._blobs = blobs
        self._author = author
        self._templates = templates

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.build_pr_branch, self.deliver]

    # ---------- build_pr_branch ----------
    @activity.defn(name=acts.BUILD_PR_BRANCH.name)
    async def build_pr_branch(self, inp: acts.BuildPrBranchInput) -> acts.PrBranchOutput:
        inp = small(inp)
        branch = ""
        try:
            branch = pr_branch_name(inp.task_id)
            default_branch = await self._remote.default_branch(inp.repo)
            title = commit_subject(
                inp.summary, scope=repo_scope(inp.repo), fallback=FALLBACK_DESCRIPTION
            )
            built = await build_pr_branch(
                inp.task_id,
                await self._groups(inp, title),
                base_sha=inp.base_sha,
                store=self._store,
                default_branch=default_branch,
                author=self._author,
            )
        except NothingToDeliverError as exc:
            raise ApplicationError(
                str(exc), type=acts.NOTHING_TO_DELIVER, non_retryable=True
            ) from exc
        except ForbiddenBranchError as exc:
            await self._refused(inp.task_id, branch, exc)
            raise _forbidden(exc) from exc
        except (GitHubError, ValueError, RepoNotFoundError) as exc:
            raise _failure(exc) from exc
        return small(
            acts.PrBranchOutput(
                branch=built.branch,
                head_sha=built.head_sha,
                base_sha=built.base_sha,
                base_branch=default_branch,
                title=title,
            )
        )

    async def _groups(self, inp: acts.BuildPrBranchInput, title: str) -> list[Group]:
        """The commits of the branch: one for the whole task, or one for each done step.

        A task without a plan never reads the table.
        """
        if inp.plan is None:
            return [Group(title, inp.head_sha)]
        rows = await self._records.step_checkpoints(inp.task_id)
        return group_by_step(
            inp.plan,
            [StepEnd(row.step_id, row.sha) for row in rows if row.step_id is not None],
            summary=inp.summary,
            head_sha=inp.head_sha,
            scope=repo_scope(inp.repo),
            fallback=FALLBACK_DESCRIPTION,
        )

    # ---------- deliver ----------
    @activity.defn(name=acts.DELIVER.name)
    async def deliver(self, inp: acts.DeliverInput) -> str:
        inp = small(inp)
        async with heartbeating():
            try:
                await self._push(inp)
                request = await self._request(inp)
                url = await create_pr(self._client, self._ledger, request)
            except ForbiddenBranchError as exc:
                await self._refused(inp.task_id, inp.branch, exc)
                raise _forbidden(exc) from exc
            except (
                GitHubError,
                SideEffectError,
                ValueError,
                RepoNotFoundError,
                TemplateError,
            ) as exc:
                raise _failure(exc) from exc
        log.info("task %s: pull request %s is open for %s", inp.task_id, url, inp.branch)
        return small(url)

    async def _push(self, inp: acts.DeliverInput) -> str:
        """Push the branch once. A row on the ledger says that it landed."""
        wanted = Wanted(
            effect_key(inp.task_id, "push", 1),
            inp.task_id,
            "push",
            {"repo": inp.repo, "branch": inp.branch, "sha": inp.head_sha},
        )

        async def write() -> str:
            pushed = await push_branch(
                inp.task_id,
                inp.branch,
                inp.branch,
                repo=inp.repo,
                store=self._store,
                remote=self._remote,
            )
            if pushed != inp.head_sha:
                # The agent can write refs in its repo. A branch that is not the one that was
                # built is not the branch of this pull request.
                raise CheckpointError(
                    f"branch {inp.branch} is at {pushed}, and not at {inp.head_sha} as built"
                )
            return pushed

        return await run_once(self._ledger, wanted, write=write)

    async def _request(self, inp: acts.DeliverInput) -> PullRequestInput:
        files = await file_stats(inp.task_id, inp.base_sha, inp.head_sha, store=self._store)
        tests = await self._last_tests(inp.task_id)
        body = render_pr_body(
            inp.issue_number, inp.summary, files, tests, templates=self._templates
        )
        return PullRequestInput(
            task_id=inp.task_id,
            repo=inp.repo,
            head=inp.branch,
            base=inp.base_branch,
            title=inp.title,
            body=body,
            draft=True,  # until a verifier has passed the work (Phase 5)
            idempotency_key=effect_key(inp.task_id, "pr", 1),
        )

    # ---------- what the body says about the tests ----------
    async def _last_tests(self, task_id: str) -> LastTestRun | None:
        row = await self._records.last_tool_call(task_id, TESTS_TOOL)
        if row is None:
            return None
        if not row.ok:
            return LastTestRun(summary=None, exit_code=row.exit_code, ran=False)
        return LastTestRun(summary=await self._summary_line(row), exit_code=row.exit_code)

    async def _summary_line(self, row: ToolCallRecord) -> str | None:
        """The counts of pytest's closing line in the output of the call, or None.

        A log that is gone is not a reason to stop a delivery that is otherwise ready, and the
        body says that it has no summary line.
        """
        if row.output_ref is None:
            return None
        try:
            text = (await self._blobs.get_by_key(row.output_ref)).decode("utf-8", errors="replace")
        except BlobError as exc:
            log.warning("the output of test call %s is not readable: %s", row.id, exc)
            return None
        return parse_pytest_summary(text)

    # ---------- failures ----------
    async def _refused(self, task_id: str, branch: str, exc: ForbiddenBranchError) -> None:
        # A failure to write the event is logged, so that it cannot replace the refusal.
        try:
            await self._records.record_event(
                task_id, BRANCH_REFUSED, {"branch": branch, "reason": str(exc)}
            )
        except Exception as record_error:
            log.warning("could not record the refused push of task %s: %s", task_id, record_error)


def _forbidden(exc: ForbiddenBranchError) -> ApplicationError:
    return ApplicationError(str(exc), type=acts.FORBIDDEN_BRANCH, non_retryable=True)


def _failure(exc: Exception) -> ApplicationError:
    """A GitHub error keeps the retry advice of the client. Every other error is a fixed fault."""
    if isinstance(exc, GitHubError):
        return github_failure(exc)
    return not_retried(exc)
