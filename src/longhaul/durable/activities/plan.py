"""The store_plan, decide_plan, post_plan and record_step activities: what happens to a plan.

The workflow cannot write to Postgres and cannot read a template, so it hands the plan to two
activities when the planner submits it (LH-501), the decision of a person on it to a third
(LH-502), and each step update of the coder to a fourth (LH-503).

`store_plan` writes version `n` of the plan to the `plans` table as `proposed`. The rows are the
history of the plan: the workflow's CarryState holds only the plan in force, and a reviewer and the
console read the table. A retry writes the same row again.

`decide_plan` records the answer of the plan gate on that row: `approved` or `rejected`, the login
of the person and the time. The first answer stands. A retry after a crash, or a second signal
that reached the workflow, finds the row decided and changes nothing, so a rejection cannot turn
into an approval after the fact.

`post_plan` is for a task of an issue. It renders the plan as a checklist (github/plan_comment.py)
and posts it with the idempotent comment write of github/side_effects.py, under the key
`<task>:plan_comment:<version>`. The key and the hidden marker in the comment make the post safe to
run again: a retry after a 502 that landed, or a run after a Continue-As-New, finds the comment
and posts nothing new (failure modes F10 and F11). The body is the same bytes on every attempt,
because `post_comment` refuses a key that is used again with other text.

`record_step` records one `update_step` call of the coder. Every update is a `step_updated` event
with the step, its status, the note and the head of the work, so the log of a task shows how the
plan went. An update to `done` also writes a `step` checkpoint at that head, which names where the
work of the step ended. The pull request has one commit for each of them (checkpoints/squash.py).
The checkpoint is written first. A retry writes the same row again, and the table keeps one for a
step and sha (db/records.py). The event is the second write, so a retry after the first has landed
adds the event once.

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

from longhaul.db.records import STEP_KIND, CheckpointRecord, PlanRecord
from longhaul.durable import acts
from longhaul.durable.activities.support import github_failure, not_retried, small
from longhaul.github.client import GitHubClient, GitHubError
from longhaul.github.inputs import CommentInput
from longhaul.github.plan_comment import render_plan_comment
from longhaul.github.side_effects import Ledger, SideEffectError, effect_key, post_comment

# The purpose part of the idempotency key of a plan comment (plan section 3.7).
PLAN_COMMENT = "plan_comment"
# The type of the event that a step update records.
STEP_EVENT = "step_updated"


class PlanRecords(Protocol):
    """The part of the recorder that store_plan and decide_plan use."""

    async def record_plan(self, record: PlanRecord) -> None: ...

    async def record_plan_decision(
        self, task_id: str, version: int, *, approved: bool, decided_by: str
    ) -> None: ...


class StepRecords(Protocol):
    """The part of the recorder that record_step uses."""

    async def record_checkpoint(self, record: CheckpointRecord) -> None: ...

    async def record_event(self, task_id: str, type: str, payload: dict[str, Any]) -> None: ...


class PlanActivities:
    """store_plan and decide_plan, with the recorder they write through. Every worker has both."""

    def __init__(self, records: PlanRecords) -> None:
        self._records = records

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.store_plan, self.decide_plan]

    @activity.defn(name=acts.STORE_PLAN.name)
    async def store_plan(self, inp: acts.StorePlanInput) -> None:
        inp = small(inp)
        await self._records.record_plan(PlanRecord(inp.task_id, inp.version, inp.plan))

    @activity.defn(name=acts.DECIDE_PLAN.name)
    async def decide_plan(self, inp: acts.DecidePlanInput) -> None:
        inp = small(inp)
        await self._records.record_plan_decision(
            inp.task_id, inp.version, approved=inp.approved, decided_by=inp.by
        )


class StepActivities:
    """record_step, with the recorder it writes through. Every worker registers it (LH-503)."""

    def __init__(self, records: StepRecords) -> None:
        self._records = records

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.record_step]

    @activity.defn(name=acts.RECORD_STEP.name)
    async def record_step(self, inp: acts.RecordStepInput) -> None:
        inp = small(inp)
        if inp.status == "done":
            await self._records.record_checkpoint(
                CheckpointRecord(
                    inp.task_id, inp.head_sha, STEP_KIND, inp.turn_index, step_id=inp.step_id
                )
            )
        await self._records.record_event(
            inp.task_id,
            STEP_EVENT,
            {
                "step_id": inp.step_id,
                "status": inp.status,
                "note": inp.note,
                "head_sha": inp.head_sha,
                "turn_index": inp.turn_index,
            },
        )


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
