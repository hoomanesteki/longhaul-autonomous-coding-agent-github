"""post_final: the report on the issue when a task ends (LH-507).

The activity reads the diff stats, renders templates/final_comment.md.j2 and posts it through the
idempotent comment write of github/side_effects.py under the key `<task>:final:1`. It runs inside
temporalio's ActivityEnvironment against a FakeGitHub, so the tests show the key, the marker and
what a retry does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from longhaul.durable.activities.outcome import FinalCommentActivities
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.checkpoints.git_store import CheckpointError, RepoNotFoundError
from longhaul.checkpoints.squash import FileStat
from longhaul.contracts import Outcome, OutcomeKind, Plan, PlanStep
from longhaul.durable import acts
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.side_effects import MemoryLedger, effect_key, marker

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
REPO = "acme/calc"
TASK = "lh-acme-calc-1"
TOKEN = "ghp_unit_test_token_0123456789abcdef"
BASE = "b" * 40
HEAD = "c" * 40
PR_URL = "https://github.com/acme/calc/pull/7"
PLAN = Plan(
    summary="add() subtracts",
    steps=[
        PlanStep(id="s1", description="Fix add()", done_when="add(2, 3) is 5", status="done"),
        PlanStep(id="s2", description="Test it", done_when="pytest -q passes"),
    ],
    acceptance_checks=["pytest -q"],
)


class Diffs:
    """A diff reader that answers from a list, or raises, and remembers what it was asked."""

    def __init__(self, files: list[FileStat] | Exception) -> None:
        self.files = files
        self.asked: list[tuple[str, str, str]] = []

    async def stats(self, task_id: str, base_sha: str, head_sha: str) -> list[FileStat]:
        self.asked.append((task_id, base_sha, head_sha))
        if isinstance(self.files, Exception):
            raise self.files
        return self.files


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


def activities(
    client: GitHubClient,
    ledger: MemoryLedger,
    diffs: Diffs | None = None,
    templates: Path | None = None,
) -> FinalCommentActivities:
    return FinalCommentActivities(
        client,
        ledger,
        diffs or Diffs([FileStat("calc.py", 2, 1)]),
        templates=templates or ROOT / "templates",
    )


def final_input(kind: OutcomeKind = OutcomeKind.STUCK, **fields: Any) -> acts.PostFinalInput:
    values: dict[str, Any] = {
        "task_id": TASK,
        "repo": REPO,
        "number": 1,
        "outcome": Outcome(kind=kind, summary="no hint in 24 hours"),
        "plan": PLAN,
        "base_sha": BASE,
        "head_sha": HEAD,
        "turns": 9,
        "cost_usd": 0.5,
    }
    return acts.PostFinalInput(**{**values, **fields})


async def post(runner: FinalCommentActivities, inp: acts.PostFinalInput) -> str:
    comment_id: str = await ActivityEnvironment().run(runner.post_final, inp)
    return comment_id


async def test_post_final_posts_the_stuck_report(client: GitHubClient, fake: FakeGitHub) -> None:
    diffs = Diffs([FileStat("calc.py", 2, 1)])
    await post(activities(client, MemoryLedger(), diffs), final_input())

    (body,) = fake.comment_bodies(REPO, 1)
    assert body.startswith("## Longhaul is stuck and has stopped\n")
    assert "- [x] **s1**: Fix add()" in body
    assert "- [ ] **s2**: Test it" in body
    assert "1 file changed, 2 additions, 1 deletion" in body
    assert marker(effect_key(TASK, "final", 1)) in body
    assert diffs.asked == [(TASK, BASE, HEAD)]


async def test_the_key_is_final_one() -> None:
    assert effect_key(TASK, acts.FINAL_COMMENT, 1) == "lh-acme-calc-1:final:1"


async def test_post_final_twice_posts_once(client: GitHubClient, fake: FakeGitHub) -> None:
    ledger = MemoryLedger()
    first = await post(activities(client, ledger), final_input())
    again = await post(activities(client, ledger), final_input())

    assert first == again
    assert fake.count_comments(REPO, 1, containing="Longhaul is stuck") == 1


async def test_a_lost_ledger_finds_the_comment_by_its_marker(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    await post(activities(client, MemoryLedger()), final_input())
    await post(activities(client, MemoryLedger()), final_input())  # a fresh, empty ledger

    assert fake.count_comments(REPO, 1, containing="Longhaul is stuck") == 1


async def test_a_write_that_landed_and_looked_like_a_failure_is_not_repeated(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    fake.after_write_fail("/comments", status=502)
    ledger = MemoryLedger()
    with pytest.raises(ApplicationError) as failed:
        await post(activities(client, ledger), final_input())
    assert failed.value.type == acts.GITHUB_RETRYABLE

    await post(activities(client, ledger), final_input())  # the retry
    assert fake.count_comments(REPO, 1, containing="Longhaul is stuck") == 1


async def test_a_delivered_task_links_its_pull_request(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    ended = Outcome(kind=OutcomeKind.PR_OPENED, pr_url=PR_URL, summary="Changed add().")
    await post(activities(client, MemoryLedger()), final_input(outcome=ended))

    (body,) = fake.comment_bodies(REPO, 1)
    assert body.startswith("## Longhaul opened a draft pull request\n")
    assert PR_URL in body


async def test_a_ready_pull_request_is_not_called_a_draft(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    ended = Outcome(kind=OutcomeKind.PR_OPENED, pr_url=PR_URL, summary="Changed add().")
    await post(activities(client, MemoryLedger()), final_input(outcome=ended, draft=False))

    (body,) = fake.comment_bodies(REPO, 1)
    assert body.startswith("## Longhaul opened a pull request\n")


async def test_a_task_that_never_had_a_checkpoint_is_reported_without_stats(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    diffs = Diffs([])
    await post(activities(client, MemoryLedger(), diffs), final_input(base_sha=None, head_sha=None))

    (body,) = fake.comment_bodies(REPO, 1)
    assert "could not be read" in body
    assert diffs.asked == []


async def test_a_repo_that_is_gone_does_not_stop_the_report(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    diffs = Diffs(RepoNotFoundError("task lh-acme-calc-1 has no checkpoint repo"))
    await post(activities(client, MemoryLedger(), diffs), final_input())

    (body,) = fake.comment_bodies(REPO, 1)
    assert "could not be read" in body
    assert "What is left" in body


async def test_a_diff_that_fails_to_read_is_retried_and_not_reported_as_missing(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    # A report without stats and a report with them are different bytes under one key, and the
    # ledger refuses the second. So only a diff that can never be read is left out.
    diffs = Diffs(CheckpointError("git diff failed: the disk is full"))
    with pytest.raises(CheckpointError):
        await post(activities(client, MemoryLedger(), diffs), final_input())

    assert fake.count_comments(REPO, 1) == 0


async def test_a_missing_template_fails_for_good(client: GitHubClient, tmp_path: Path) -> None:
    with pytest.raises(ApplicationError) as failed:
        await post(activities(client, MemoryLedger(), templates=tmp_path), final_input())
    assert failed.value.non_retryable
    assert failed.value.type == "TemplateNotFound"


async def test_an_issue_that_does_not_exist_fails_for_good(client: GitHubClient) -> None:
    with pytest.raises(ApplicationError) as failed:
        await post(activities(client, MemoryLedger()), final_input(number=99))
    assert failed.value.non_retryable


def test_the_activity_is_the_one_the_workflow_calls() -> None:
    runner = FinalCommentActivities(None, MemoryLedger(), Diffs([]), templates=ROOT)  # type: ignore[arg-type]
    assert [fn.__temporal_activity_definition.name for fn in runner.activities] == [  # type: ignore[attr-defined]
        acts.POST_FINAL.name
    ]
