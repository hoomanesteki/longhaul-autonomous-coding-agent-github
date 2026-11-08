"""The check_budget activity (LH-702): the numbers behind a pause, from the ledger.

It runs inside temporalio's ActivityEnvironment with the memory ledger on the checked-in prices.
The workflow hands it the limits, the top-ups, the turn and the size of the last request, and it
adds the ledger's totals and the worst case of the next call. The wall clock is not here: the
workflow judges it from its own time.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from longhaul.cost.budget import BudgetNumbers, BudgetReason, BudgetVerdict
from longhaul.durable.activities.cost import BudgetActivities, BudgetCommentActivities
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.agent.loop import LoopConfig
from longhaul.contracts import Budgets, Role, Usage
from longhaul.cost.ledger import MemoryLedger
from longhaul.cost.pricing import Pricing
from longhaul.durable import acts
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.side_effects import MemoryLedger as EffectLedger
from longhaul.github.side_effects import effect_key, marker

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
PRICES = Pricing.load(ROOT / "config" / "pricing.yaml")
SONNET = "claude-sonnet-5-5"
NOW = datetime(2026, 11, 3, 15, 30, tzinfo=UTC)
# One call of 10,000 tokens in and 1,000 out costs 0.02 + 0.01 on sonnet.
CFG = LoopConfig(model=SONNET, max_tokens_per_turn=1_000, max_tokens_retry=5_000)
WORST_CASE = Decimal("0.030000")


def config_for(role: Role) -> LoopConfig:
    return CFG


def ask(**changes: object) -> acts.CheckBudgetInput:
    values: dict[str, object] = {
        "task_id": "lh-acme-calc-42",
        "repo": "acme/calc",
        "role": Role.CODER,
        "turn_index": 3,
        "limits": Budgets(task_usd=1.0, task_turns=10, task_wall_minutes=240, daily_repo_usd=2.0),
        "last_input_tokens": 10_000,
        "now": NOW,
    }
    values.update(changes)
    return acts.CheckBudgetInput(**values)  # type: ignore[arg-type]


async def run(ledger: MemoryLedger, inp: acts.CheckBudgetInput) -> BudgetVerdict:
    activities = BudgetActivities(PRICES, ledger, config_for)
    verdict: BudgetVerdict = await ActivityEnvironment().run(activities.check_budget, inp)
    return verdict


async def spend(ledger: MemoryLedger, task_id: str, repo: str, dollars: int) -> Decimal:
    """A row worth `dollars` on sonnet: a million input tokens cost 2.00, so half of them 1.00."""
    return await ledger.record(task_id, repo, SONNET, 0, 1, Usage(input_tokens=dollars * 500_000))


async def test_a_task_with_room_left_is_ok_and_the_numbers_show_the_worst_case() -> None:
    ledger = MemoryLedger(PRICES)
    verdict = await run(ledger, ask())
    assert verdict.ok and verdict.reason is None
    assert Decimal(str(verdict.numbers.projected_usd)) == WORST_CASE
    assert verdict.numbers.task_usd_limit == 1.0 and verdict.numbers.turns_limit == 10


async def test_the_task_total_comes_from_the_ledger() -> None:
    ledger = MemoryLedger(PRICES)
    await spend(ledger, "lh-acme-calc-42", "acme/calc", 1)  # 1.00 spent, 0.03 more is over 1.00
    verdict = await run(ledger, ask())
    assert not verdict.ok and verdict.reason is BudgetReason.TASK_USD
    assert verdict.numbers.spent_task_usd == 1.0


async def test_a_top_up_makes_room_for_the_next_call() -> None:
    ledger = MemoryLedger(PRICES)
    await spend(ledger, "lh-acme-calc-42", "acme/calc", 1)
    assert (await run(ledger, ask(extra_usd=0.5))).ok


async def test_the_workflows_running_total_counts_when_the_ledger_knows_less() -> None:
    # A worker with no database has a ledger that reads zero, and the workflow still adds up
    # what each llm_turn reported.
    ledger = MemoryLedger(PRICES)
    verdict = await run(ledger, ask(spent_task_usd=0.99))
    assert verdict.reason is BudgetReason.TASK_USD


async def test_the_turn_limit_is_judged_on_the_turn_index() -> None:
    ledger = MemoryLedger(PRICES)
    verdict = await run(ledger, ask(turn_index=10))
    assert verdict.reason is BudgetReason.TASK_TURNS
    assert (await run(ledger, ask(turn_index=10, extra_turns=5))).ok


async def test_the_daily_total_is_the_repos_spend_since_midnight_utc() -> None:
    ledger = MemoryLedger(PRICES)
    for n in range(3):
        await spend(ledger, f"lh-other-{n}", "acme/calc", 1)  # 3.00 today in the repo
    verdict = await run(ledger, ask(now=datetime.now(UTC)))
    assert verdict.reason is BudgetReason.DAILY_REPO_USD
    assert verdict.numbers.spent_repo_today_usd == 3.0
    assert verdict.can_wait


async def test_yesterdays_rows_are_not_part_of_the_day() -> None:
    ledger = MemoryLedger(PRICES)
    for n in range(3):
        await spend(ledger, f"lh-other-{n}", "acme/calc", 1)
    # The workflow says it is the day after the rows were written.
    verdict = await run(ledger, ask(now=datetime.now(UTC) + timedelta(days=1)))
    assert verdict.ok


async def test_another_repos_rows_are_not_part_of_the_day() -> None:
    ledger = MemoryLedger(PRICES)
    for n in range(3):
        await spend(ledger, f"lh-other-{n}", "acme/elsewhere", 1)
    assert (await run(ledger, ask(now=datetime.now(UTC)))).ok


async def test_the_planner_is_priced_with_its_own_model() -> None:
    def per_role(role: Role) -> LoopConfig:
        return LoopConfig(
            model="claude-haiku-5-5" if role is Role.PLANNER else SONNET, max_tokens_per_turn=1_000
        )

    ledger = MemoryLedger(PRICES)
    activities = BudgetActivities(PRICES, ledger, per_role)
    verdict: BudgetVerdict = await ActivityEnvironment().run(
        activities.check_budget, ask(role=Role.PLANNER)
    )
    # 10,000 tokens in at 0.10 and 1,000 out at 0.50 per million.
    assert Decimal(str(verdict.numbers.projected_usd)) == Decimal("0.001500")


async def test_a_model_without_a_price_fails_for_good() -> None:
    def odd(role: Role) -> LoopConfig:
        return LoopConfig(model="claude-unpriced-1")

    activities = BudgetActivities(PRICES, MemoryLedger(PRICES), odd)
    with pytest.raises(ApplicationError) as failed:
        await ActivityEnvironment().run(activities.check_budget, ask())
    assert failed.value.non_retryable and failed.value.type == "PricingError"


def test_the_activity_is_registered_under_its_name() -> None:
    activities = BudgetActivities(PRICES, MemoryLedger(PRICES), config_for)
    assert [a.__name__ for a in activities.activities] == ["check_budget"]
    assert acts.CHECK_BUDGET.name == "check_budget"


# ---------- post_budget ----------
REPO = "acme/calc"
TASK = "lh-acme-calc-1"
TOKEN = "ghp_unit_test_token_0123456789abcdef"
NUMBERS = BudgetNumbers(
    spent_task_usd=0.99, projected_usd=0.03, task_usd_limit=1.0, turns_done=3, turns_limit=10
)


@pytest.fixture
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO)
    github.seed_issue(REPO, "add() is wrong")  # issue 1
    return github


@pytest.fixture
async def client(fake: FakeGitHub) -> AsyncIterator[GitHubClient]:
    async with GitHubClient(FAKE_BASE_URL, PatAuth(TOKEN), transport=fake.transport()) as gh:
        yield gh


def post_input(pause: int = 1) -> acts.PostBudgetInput:
    return acts.PostBudgetInput(
        task_id=TASK,
        repo=REPO,
        number=1,
        pause=pause,
        reason=BudgetReason.TASK_USD,
        numbers=NUMBERS,
        wait_s=24 * 3600,
    )


async def post(activities: BudgetCommentActivities, inp: acts.PostBudgetInput) -> str:
    comment_id: str = await ActivityEnvironment().run(activities.post_budget, inp)
    return comment_id


def budget_comments(client: GitHubClient, ledger: EffectLedger) -> BudgetCommentActivities:
    return BudgetCommentActivities(client, ledger, templates=ROOT / "templates")


async def test_post_budget_posts_the_comment_under_the_key_of_the_pause(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    await post(budget_comments(client, EffectLedger()), post_input())

    (body,) = fake.comment_bodies(REPO, 1)
    assert body.startswith("## Longhaul paused: task budget")
    assert "$0.99" in body and "/longhaul budget +2" in body
    assert marker(effect_key(TASK, "budget", 1)) in body
    assert effect_key(TASK, "budget", 1) == "lh-acme-calc-1:budget:1"


async def test_post_budget_twice_posts_once(client: GitHubClient, fake: FakeGitHub) -> None:
    ledger = EffectLedger()
    first = await post(budget_comments(client, ledger), post_input())
    again = await post(budget_comments(client, ledger), post_input())

    assert first == again
    assert fake.count_comments(REPO, 1, containing=":budget:1") == 1


async def test_the_second_pause_is_a_comment_of_its_own(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    ledger = EffectLedger()
    await post(budget_comments(client, ledger), post_input(1))
    await post(budget_comments(client, ledger), post_input(2))

    assert fake.count_comments(REPO, 1, containing=":budget:") == 2


async def test_a_daily_pause_says_when_it_ends(client: GitHubClient, fake: FakeGitHub) -> None:
    inp = acts.PostBudgetInput(
        task_id=TASK,
        repo=REPO,
        number=1,
        pause=1,
        reason=BudgetReason.DAILY_REPO_USD,
        numbers=NUMBERS,
        resume_at=datetime(2026, 11, 4, tzinfo=UTC),
    )
    await post(budget_comments(client, EffectLedger()), inp)

    (body,) = fake.comment_bodies(REPO, 1)
    assert "2026-11-04 00:00 UTC" in body


async def test_a_pause_that_cannot_be_told_fails_for_good() -> None:
    # A task limit with no wait is a bug in the caller, and asking again repeats it.
    inp = acts.PostBudgetInput(
        task_id=TASK, repo=REPO, number=1, pause=1, reason=BudgetReason.TASK_USD, numbers=NUMBERS
    )
    async with GitHubClient(FAKE_BASE_URL, PatAuth(TOKEN), transport=FakeGitHub().transport()) as c:
        with pytest.raises(ApplicationError) as failed:
            await post(budget_comments(c, EffectLedger()), inp)
    assert failed.value.non_retryable


def test_post_budget_is_registered_under_its_name(client: GitHubClient) -> None:
    activities = budget_comments(client, EffectLedger())
    assert [a.__name__ for a in activities.activities] == ["post_budget"]
    assert acts.POST_BUDGET.name == "post_budget"
