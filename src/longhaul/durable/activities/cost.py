"""The check_budget and post_budget activities: what a budget decides, and what it says (LH-702).

Before every model turn of a task that enforces budgets, the workflow calls `check_budget`. The
activity adds what the ledger holds to the worst case of the call about to be made, and hands the
sums to the pure rules of cost/budget.py. The workflow cannot read a database, and it cannot read
the config to find the model of a role and its output cap, so the activity does both.

**What is compared.** The task's spend is the larger of the ledger's total and the workflow's own
running total (acts.CheckBudgetInput.spent_task_usd). The ledger is the truth, because it holds a
call that was billed and then retried. A worker with no database has a ledger that reads zero,
and the running total keeps the check honest there. The spend of the repo is the ledger's sum
since 00:00 UTC of the day of `now`, the clock of the workflow, so a task that has waited for
midnight sees the new day.

**The worst case of the next call** is the input of the last request at the full input price,
plus `max_tokens_per_turn` of output at the output price. Both are priced with the model of the
role (cost.pricing). A model with no price fails the activity for good: the worker checks every
configured model at the start, so this means the config changed under a running task.

`post_budget` is for a task of an issue. It renders the comment (github/budget_comment.py) and
posts it with the idempotent comment write of github/side_effects.py, under the key
`<task>:budget:<pause>`. Like `post_plan`, a retry after a 502 that landed, or a run after a
Continue-As-New, finds the comment and posts nothing new. The body is the same bytes on every
attempt because `post_comment` refuses a key that is used again with other text.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import TemplateError
from temporalio import activity

from longhaul.agent.loop import LoopConfig
from longhaul.contracts import Role, Usage
from longhaul.cost import budget
from longhaul.cost.budget import BudgetVerdict
from longhaul.cost.ledger import Spending
from longhaul.cost.pricing import Pricing, PricingError
from longhaul.durable import acts
from longhaul.durable.activities.support import github_failure, not_retried, small
from longhaul.github.budget_comment import BUDGET_COMMENT, render_budget_comment
from longhaul.github.client import GitHubClient, GitHubError
from longhaul.github.inputs import CommentInput
from longhaul.github.side_effects import Ledger, SideEffectError, effect_key, post_comment


class BudgetActivities:
    """check_budget, with what it needs: the prices, the ledger's sums and the config of a role.

    `config_for` is the one the other model activities use, so the model and the output cap are
    those of the call that will follow.
    """

    def __init__(
        self, pricing: Pricing, spending: Spending, config_for: Callable[[Role], LoopConfig]
    ) -> None:
        self._pricing = pricing
        self._spending = spending
        self._config_for = config_for

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.check_budget]

    @activity.defn(name=acts.CHECK_BUDGET.name)
    async def check_budget(self, inp: acts.CheckBudgetInput) -> BudgetVerdict:
        inp = small(inp)
        cfg = self._config_for(inp.role)
        try:
            projected = self._pricing.cost(
                Usage(input_tokens=inp.last_input_tokens, output_tokens=cfg.max_tokens_per_turn),
                cfg.model,
            )
        except PricingError as exc:
            raise not_retried(exc) from exc
        ledger_total = await self._spending.spent_by_task(inp.task_id)
        spent_task = max(ledger_total, Decimal(repr(inp.spent_task_usd)))
        today = await self._spending.spent_by_repo_since(inp.repo, budget.day_start(inp.now))
        return budget.check(
            inp.limits,
            extra_usd=inp.extra_usd,
            extra_turns=inp.extra_turns,
            turn_index=inp.turn_index,
            spent_task=spent_task,
            spent_repo_today=today,
            projected=projected,
        )


class BudgetCommentActivities:
    """post_budget, with what it needs: the GitHub client, the ledger and the templates folder.

    Only a worker with a GitHub token registers it, as it does the other writes. `ledger` is the
    one that the other writes of the worker share, so that a key is held and recorded once.
    `templates` is the folder that holds budget_comment.md.j2.
    """

    def __init__(self, client: GitHubClient, ledger: Ledger, *, templates: Path) -> None:
        self._client = client
        self._ledger = ledger
        self._templates = templates

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.post_budget]

    @activity.defn(name=acts.POST_BUDGET.name)
    async def post_budget(self, inp: acts.PostBudgetInput) -> str:
        inp = small(inp)
        wait = None if inp.wait_s is None else timedelta(seconds=inp.wait_s)
        try:
            request = CommentInput(
                task_id=inp.task_id,
                repo=inp.repo,
                number=inp.number,
                body=render_budget_comment(
                    inp.reason,
                    inp.numbers,
                    inp.pause,
                    wait=wait,
                    resume_at=inp.resume_at,
                    templates=self._templates,
                ),
                idempotency_key=effect_key(inp.task_id, BUDGET_COMMENT, inp.pause),
            )
            comment_id = await post_comment(self._client, self._ledger, request)
        except GitHubError as exc:
            raise github_failure(exc) from exc
        except (SideEffectError, ValueError, TemplateError) as exc:
            raise not_retried(exc) from exc
        return small(comment_id)
