"""The post_final activity: the report on the issue when a task ends (LH-507).

The workflow cannot read a template or run git, so it hands the outcome to this activity. The
activity reads the diff stats of the work, renders the comment (github/final_comment.py) and posts
it with the idempotent comment write of github/side_effects.py under the key `<task>:final:1`. A
task ends once and posts one such comment: a retry after a 502 that landed, or a run after a
Continue-As-New, finds the comment by its key and its hidden marker and posts nothing new
(failure modes F10 and F11).

The comment is the same bytes on every attempt, because `post_comment` refuses a key that is used
again with other text. That settles how a diff that cannot be read is handled. The commits of the
work never change once the task has ended, so a diff that cannot be read for a reason that stays
(the task made no checkpoint, the repo is gone, a sha is not valid) is left out and the comment
says so, and the same is true on the next attempt. A diff that fails for a reason that may pass,
such as a full disk or a git process that was killed, fails the activity, and Temporal runs it
again. Leaving the stats out for such an error would post a report that the retry could not
replace.

Failures. A GitHub error is retryable or not as the client classified it (support.github_failure).
A request that no retry can fix, such as a template that is missing or a key that was used for
another comment, is not retried.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from jinja2 import TemplateError
from temporalio import activity

from longhaul.checkpoints.git_store import GitCheckpointStore, RepoNotFoundError
from longhaul.checkpoints.squash import FileStat, file_stats
from longhaul.durable import acts
from longhaul.durable.activities.support import github_failure, not_retried, small
from longhaul.github.client import GitHubClient, GitHubError
from longhaul.github.final_comment import render_final_comment
from longhaul.github.inputs import CommentInput
from longhaul.github.side_effects import Ledger, SideEffectError, effect_key, post_comment

log = logging.getLogger(__name__)


class DiffReader(Protocol):
    """Where the final comment gets the diff stats of the work."""

    async def stats(self, task_id: str, base_sha: str, head_sha: str) -> list[FileStat]: ...


class StoreDiffs:
    """The diff stats of a task, read from its bare repo on this host."""

    def __init__(self, store: GitCheckpointStore) -> None:
        self._store = store

    async def stats(self, task_id: str, base_sha: str, head_sha: str) -> list[FileStat]:
        return await file_stats(task_id, base_sha, head_sha, store=self._store)


class FinalCommentActivities:
    """post_final, with what it needs: the GitHub client, the ledger, the diff and the templates.

    Only a worker with a GitHub token registers it, as it does the other writes. `ledger` is the
    one that the other writes of the worker share, so that a key is held and recorded once.
    `templates` is the folder that holds final_comment.md.j2.
    """

    def __init__(
        self, client: GitHubClient, ledger: Ledger, diffs: DiffReader, *, templates: Path
    ) -> None:
        self._client = client
        self._ledger = ledger
        self._diffs = diffs
        self._templates = templates

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.post_final]

    @activity.defn(name=acts.POST_FINAL.name)
    async def post_final(self, inp: acts.PostFinalInput) -> str:
        inp = small(inp)
        try:
            body = render_final_comment(
                inp.outcome,
                plan=inp.plan,
                files=await self._files(inp),
                turns=inp.turns,
                cost_usd=inp.cost_usd,
                draft=inp.draft,
                templates=self._templates,
            )
            request = CommentInput(
                task_id=inp.task_id,
                repo=inp.repo,
                number=inp.number,
                body=body,
                idempotency_key=effect_key(inp.task_id, acts.FINAL_COMMENT, 1),
            )
            comment_id = await post_comment(self._client, self._ledger, request)
        except GitHubError as exc:
            raise github_failure(exc) from exc
        except (SideEffectError, ValueError, TemplateError) as exc:
            raise not_retried(exc) from exc
        return small(comment_id)

    async def _files(self, inp: acts.PostFinalInput) -> list[FileStat] | None:
        """The diff stats, or None when they can never be read (see the module docstring)."""
        if inp.base_sha is None or inp.head_sha is None:
            return None
        try:
            return await self._diffs.stats(inp.task_id, inp.base_sha, inp.head_sha)
        except (RepoNotFoundError, ValueError) as exc:
            log.warning("no diff stats for the final comment of task %s: %s", inp.task_id, exc)
            return None
