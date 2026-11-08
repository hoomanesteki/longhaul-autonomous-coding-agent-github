"""The store_plan and post_plan activities: what happens to a plan once the planner submits it.

The workflow cannot write to Postgres and cannot read a template, so it hands the plan to two
activities (LH-501).

`store_plan` writes version `n` of the plan to the `plans` table as `proposed`. The rows are the
history of the plan: the workflow's CarryState holds only the plan in force, and a reviewer, the
console and the plan gate (LH-502) read the table. A retry writes the same row again.

`post_plan` is for a task of an issue. It renders the plan as a checklist (github/plan_comment.py)
and posts it with the idempotent comment write of github/side_effects.py, under the key
`<task>:plan_comment:<version>`. The key and the hidden marker in the comment make the post safe to
run again: a retry after a 502 that landed, or a run after a Continue-As-New, finds the comment
and posts nothing new (failure modes F10 and F11). The body is the same bytes on every attempt,
because `post_comment` refuses a key that is used again with other text.

Failures. A GitHub error is retryable or not as the client classified it (support.github_failure).
A request that no retry can fix, such as a template that is missing or a key that was used for
another comment, is not retried.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from jinja2 import TemplateError
from temporalio import activity

from longhaul.db.records import PlanRecord
from longhaul.durable import acts
from longhaul.durable.activities.support import github_failure, not_retried, small
from longhaul.github.client import GitHubClient, GitHubError
from longhaul.github.inputs import CommentInput
from longhaul.github.plan_comment import render_plan_comment
from longhaul.github.side_effects import Ledger, SideEffectError, effect_key, post_comment

# The purpose part of the idempotency key of a plan comment (plan section 3.7).
PLAN_COMMENT = "plan_comment"


class PlanRecords(Protocol):
    """The part of the recorder that store_plan uses."""

    async def record_plan(self, record: PlanRecord) -> None: ...


class PlanActivities:
    """store_plan, with the recorder it writes through. Every worker registers it."""

    def __init__(self, records: PlanRecords) -> None:
        self._records = records

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.store_plan]

    @activity.defn(name=acts.STORE_PLAN.name)
    async def store_plan(self, inp: acts.StorePlanInput) -> None:
        inp = small(inp)
        await self._records.record_plan(PlanRecord(inp.task_id, inp.version, inp.plan))


class PlanCommentActivities:
    """post_plan, with what it needs: the GitHub client, the ledger and the templates folder.

    Only a worker with a GitHub token registers it, as it does the other writes. `ledger` is the
    one that the other writes of the worker share, so that a key is held and recorded once.
    `templates` is the folder that holds plan_comment.md.j2.
    """

    def __init__(self, client: GitHubClient, ledger: Ledger, *, templates: Path) -> None:
        self._client = client
        self._ledger = ledger
        self._templates = templates

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.post_plan]

    @activity.defn(name=acts.POST_PLAN.name)
    async def post_plan(self, inp: acts.PostPlanInput) -> str:
        inp = small(inp)
        try:
            request = CommentInput(
                task_id=inp.task_id,
                repo=inp.repo,
                number=inp.number,
                body=render_plan_comment(inp.plan, inp.version, templates=self._templates),
                idempotency_key=effect_key(inp.task_id, PLAN_COMMENT, inp.version),
            )
            comment_id = await post_comment(self._client, self._ledger, request)
        except GitHubError as exc:
            raise github_failure(exc) from exc
        except (SideEffectError, ValueError, TemplateError) as exc:
            raise not_retried(exc) from exc
        return small(comment_id)
