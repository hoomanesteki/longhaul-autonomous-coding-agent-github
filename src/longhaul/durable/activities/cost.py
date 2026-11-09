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

**The first request of a transcript** has no last request. The planner, the coder and the
verifier each start one, and the verifier's first request carries the review brief, which can be
large. Priced as output only, a call could pass the check and end the task over its limit. So
when the workflow sends no input size it sends what the request is made of, and the activity
estimates it the way the llm activity does: the system prompt and the tool list of the role, the
first message, the plan, and the memory block and the review outputs at the sizes of their blobs
(the review outputs are cut to the caps that llm_turn applies). The estimate counts characters at
the ratio of the task's token meter, which is rough but never zero, and it leaves out the hints
that a resumed coder is shown. A check with neither a size nor a task prices the output alone, as
before.

`post_budget` is for a task of an issue. It renders the comment (github/budget_comment.py) and
posts it with the idempotent comment write of github/side_effects.py, under the key
`<task>:budget:<pause>`. Like `post_plan`, a retry after a 502 that landed, or a run after a
Continue-As-New, finds the comment and posts nothing new. The body is the same bytes on every
attempt because `post_comment` refuses a key that is used again with other text.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from jinja2 import TemplateError
from temporalio import activity

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import (
    build_planned_system_prompt,
    build_system_prompt,
    first_user_message,
    render_plan,
    with_plan,
)
from longhaul.agent.turns import append_user_text
from longhaul.context.tokens import DEFAULT_CHARS_PER_TOKEN, count_request_chars
from longhaul.contracts import Message, Role, ToolSpec, Usage
from longhaul.cost import budget
from longhaul.cost.budget import BudgetVerdict
from longhaul.cost.ledger import Spending
from longhaul.cost.pricing import Pricing, PricingError
from longhaul.durable import acts
from longhaul.durable.activities.support import Meters, github_failure, not_retried, small
from longhaul.github.budget_comment import BUDGET_COMMENT, render_budget_comment
from longhaul.github.client import GitHubClient, GitHubError
from longhaul.github.inputs import CommentInput
from longhaul.github.side_effects import Ledger, SideEffectError, effect_key, post_comment
from longhaul.roles import verifier


class BudgetActivities:
    """check_budget, with what it needs: the prices, the ledger's sums and the config of a role.

    `config_for` is the one the other model activities use, so the model and the output cap are
    those of the call that will follow. `tools_for` (the llm activity's) and `meters` are for the
    estimate of a first request: without them the tool list counts for nothing, and the ratio of
    characters to tokens is the default one.
    """

    def __init__(
        self,
        pricing: Pricing,
        spending: Spending,
        config_for: Callable[[Role], LoopConfig],
        *,
        tools_for: Callable[..., Sequence[ToolSpec]] | None = None,
        meters: Meters | None = None,
    ) -> None:
        self._pricing = pricing
        self._spending = spending
        self._config_for = config_for
        self._tools_for = tools_for
        self._meters = meters if meters is not None else Meters()

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.check_budget]

    @activity.defn(name=acts.CHECK_BUDGET.name)
    async def check_budget(self, inp: acts.CheckBudgetInput) -> BudgetVerdict:
        inp = small(inp)
        cfg = self._config_for(inp.role)
        cap = cfg.max_tokens_retry if inp.high_output_cap else cfg.max_tokens_per_turn
        try:
            projected = self._pricing.cost(
                Usage(input_tokens=self._input_tokens(inp, cfg), output_tokens=cap), cfg.model
            )
        except PricingError as exc:
            raise not_retried(exc) from exc
        except ValueError as exc:  # a role with no prompt
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

    def _input_tokens(self, inp: acts.CheckBudgetInput, cfg: LoopConfig) -> int:
        """The size of the next request: the last one, or the estimate of a first one."""
        if inp.last_input_tokens > 0 or inp.spec is None:
            return inp.last_input_tokens
        planned = inp.role is Role.CODER and inp.plan is not None
        system = (
            build_planned_system_prompt(inp.spec)
            if planned
            else build_system_prompt(inp.role, inp.spec)
        )
        messages: list[Message] = append_user_text(
            [], first_user_message(inp.role, planned=planned)
        )
        if planned and inp.plan is not None:
            messages = with_plan(messages, inp.plan)
        if inp.role is Role.VERIFIER and inp.plan is not None:
            messages = append_user_text(messages, render_plan(inp.plan))
        tools = self._tools_for(inp.role, planned=planned) if self._tools_for else []
        per_token = self._meters.for_task(inp.task_id).chars_per_token
        tokens = math.ceil(count_request_chars(system, messages, tools) / per_token)
        # The memory block is text in a blob, the size of which is in the ref.
        if inp.memory_ref is not None:
            tokens += math.ceil(inp.memory_ref.size / per_token)
        if inp.role is Role.VERIFIER and inp.review is not None:
            tokens += _review_tokens(inp.review, cfg, per_token)
        return tokens


# Room for the truncation marker of a cut output and the line that names its command.
SHOWN_EXTRA_CHARS = 200


def _review_tokens(review: acts.ReviewBrief, cfg: LoopConfig, per_token: float) -> int:
    """The most that the pre-check and the diff add to the first message of the verifier.

    llm_turn cuts each output before the model sees it (shape_output), and the cut is in
    characters: the cap times DEFAULT_CHARS_PER_TOKEN. The task's meter can have learned fewer
    characters per token than that, so the cut text is counted at the task's ratio and not as
    the cap.
    """

    def shown(run: acts.CheckRun, cap: int) -> int:
        size = run.output_ref.size if run.output_ref is not None else 0
        kept = min(size, math.floor(cap * DEFAULT_CHARS_PER_TOKEN) + SHOWN_EXTRA_CHARS)
        return math.ceil(kept / per_token)

    total = sum(shown(run, verifier.CHECK_OUTPUT_TOKENS) for run in review.checks)
    if review.diff is not None:
        total += shown(review.diff, cfg.tool_output_max_tokens)
    return total


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
