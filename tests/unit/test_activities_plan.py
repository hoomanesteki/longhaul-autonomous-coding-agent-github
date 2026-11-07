"""store_plan and post_plan: how a plan is kept and told to the issue (LH-501).

Both run inside temporalio's ActivityEnvironment. store_plan writes to the memory recorder.
post_plan posts to a FakeGitHub through the idempotent write of github/side_effects.py, so the
tests also show the key, the marker and what a retry does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.contracts import Plan, PlanStep
from longhaul.db.records import CheckpointRecord, EventRecord, MemoryRecorder, PlanRecord
from longhaul.durable import acts
from longhaul.durable.activities.plan import PlanActivities, PlanCommentActivities
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.side_effects import MemoryLedger, effect_key, marker

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
REPO = "acme/calc"
TASK = "lh-acme-calc-1"
TOKEN = "ghp_unit_test_token_0123456789abcdef"
PLAN = Plan(
    summary="add() subtracts",
    steps=[
        PlanStep(id="s1", description="Fix add()", done_when="add(2, 3) is 5"),
        PlanStep(id="s2", description="Test it", done_when="pytest -q passes"),
    ],
    acceptance_checks=["pytest -q"],
)


SHA = "a" * 40


def step_input(status: str = "done", **fields: Any) -> acts.RecordStepInput:
    values: dict[str, Any] = {
        "task_id": TASK,
        "step_id": "s1",
        "status": status,
        "note": "the tests of add() pass",
        "head_sha": SHA,
        "turn_index": 6,
    }
    return acts.RecordStepInput(**{**values, **fields})


class Broken(MemoryRecorder):
    async def record_plan(self, record: PlanRecord) -> None:
        raise ConnectionError("the database is down")


# ---------- store_plan ----------
async def test_store_plan_writes_the_proposed_plan() -> None:
    recorder = MemoryRecorder()
    activities = PlanActivities(recorder)
    await ActivityEnvironment().run(
        activities.store_plan, acts.StorePlanInput(task_id=TASK, version=1, plan=PLAN)
    )
    assert recorder.plans == {(TASK, 1): PlanRecord(TASK, 1, PLAN, status="proposed")}


async def test_store_plan_twice_leaves_one_row() -> None:
    recorder = MemoryRecorder()
    inp = acts.StorePlanInput(task_id=TASK, version=1, plan=PLAN)
    for _ in range(2):
        await ActivityEnvironment().run(PlanActivities(recorder).store_plan, inp)
    assert len(recorder.plans) == 1


async def test_a_database_that_is_down_is_left_to_the_retry_policy() -> None:
    inp = acts.StorePlanInput(task_id=TASK, version=1, plan=PLAN)
    with pytest.raises(ConnectionError):
        await ActivityEnvironment().run(PlanActivities(Broken()).store_plan, inp)


def test_the_store_activity_is_the_one_the_workflow_calls() -> None:
    activities = PlanActivities(MemoryRecorder())
    assert [fn.__temporal_activity_definition.name for fn in activities.activities] == [  # type: ignore[attr-defined]
        acts.STORE_PLAN.name
    ]


# ---------- post_plan ----------
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


def comments(client: GitHubClient, ledger: MemoryLedger, templates: Path | None = None) -> Any:
    return PlanCommentActivities(client, ledger, templates=templates or ROOT / "templates")


def post_input(version: int = 1, plan: Plan = PLAN) -> acts.PostPlanInput:
    return acts.PostPlanInput(task_id=TASK, repo=REPO, number=1, version=version, plan=plan)


async def post(activities: Any, inp: acts.PostPlanInput) -> str:
    comment_id: str = await ActivityEnvironment().run(activities.post_plan, inp)
    return comment_id


async def test_post_plan_posts_the_rendered_checklist(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    await post(comments(client, MemoryLedger()), post_input())

    (body,) = fake.comment_bodies(REPO, 1)
    assert body.startswith("## Plan, version 1\n")
    assert "- [ ] **s1**: Fix add()" in body
    assert "- [ ] pytest -q" in body
    # The key rides in the comment as the hidden marker, so a lost ledger still finds it.
    assert marker(effect_key(TASK, "plan_comment", 1)) in body


async def test_the_key_names_the_plan_and_its_version() -> None:
    assert effect_key(TASK, "plan_comment", 1) == "lh-acme-calc-1:plan_comment:1"


async def test_post_plan_twice_posts_once(client: GitHubClient, fake: FakeGitHub) -> None:
    ledger = MemoryLedger()
    first = await post(comments(client, ledger), post_input())
    again = await post(comments(client, ledger), post_input())

    assert first == again
    assert fake.count_comments(REPO, 1, containing="Plan, version 1") == 1


async def test_a_lost_ledger_finds_the_comment_by_its_marker(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    first = await post(comments(client, MemoryLedger()), post_input())
    again = await post(comments(client, MemoryLedger()), post_input())  # a fresh, empty ledger

    assert first == again
    assert fake.count_comments(REPO, 1, containing="Plan, version 1") == 1


async def test_a_write_that_landed_and_looked_like_a_failure_is_not_repeated(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    fake.after_write_fail("/comments", status=502)
    ledger = MemoryLedger()
    with pytest.raises(ApplicationError) as failed:
        await post(comments(client, ledger), post_input())
    assert failed.value.type == acts.GITHUB_RETRYABLE
    assert not failed.value.non_retryable

    await post(comments(client, ledger), post_input())  # the retry
    assert fake.count_comments(REPO, 1, containing="Plan, version 1") == 1


async def test_the_next_version_of_a_plan_is_a_comment_of_its_own(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    ledger = MemoryLedger()
    first = await post(comments(client, ledger), post_input(1))
    second = await post(comments(client, ledger), post_input(2))

    assert first != second
    assert fake.count_comments(REPO, 1, containing="Plan, version 1") == 1
    assert fake.count_comments(REPO, 1, containing="Plan, version 2") == 1


async def test_a_missing_template_fails_for_good(client: GitHubClient, tmp_path: Path) -> None:
    with pytest.raises(ApplicationError) as failed:
        await post(comments(client, MemoryLedger(), templates=tmp_path), post_input())
    assert failed.value.non_retryable
    assert failed.value.type == "TemplateNotFound"


async def test_an_issue_that_does_not_exist_fails_for_good(client: GitHubClient) -> None:
    inp = post_input().model_copy(update={"number": 99})
    with pytest.raises(ApplicationError) as failed:
        await post(comments(client, MemoryLedger()), inp)
    assert failed.value.non_retryable


async def test_a_github_that_is_down_is_retried(client: GitHubClient, fake: FakeGitHub) -> None:
    fake.fail_next("/comments", status=503)
    with pytest.raises(ApplicationError) as failed:
        await post(comments(client, MemoryLedger()), post_input())
    assert failed.value.type == acts.GITHUB_RETRYABLE
    assert not failed.value.non_retryable


def test_the_comment_activity_is_the_one_the_workflow_calls(client: GitHubClient) -> None:
    activities = comments(client, MemoryLedger())
    assert [fn.__temporal_activity_definition.name for fn in activities.activities] == [
        acts.POST_PLAN.name
    ]


# ---------- record_step (LH-503) ----------
async def test_a_step_that_is_done_gets_a_step_checkpoint_at_the_head() -> None:
    recorder = MemoryRecorder()
    await ActivityEnvironment().run(PlanActivities(recorder).record_step, step_input("done"))

    assert recorder.checkpoints == [CheckpointRecord(TASK, SHA, "step", turn_index=6, step_id="s1")]


@pytest.mark.parametrize("status", ["todo", "doing", "skipped"])
async def test_a_step_that_is_not_done_gets_no_checkpoint(status: str) -> None:
    recorder = MemoryRecorder()
    await ActivityEnvironment().run(PlanActivities(recorder).record_step, step_input(status))
    assert recorder.checkpoints == []


async def test_every_update_is_an_event_with_the_note() -> None:
    recorder = MemoryRecorder()
    activities = PlanActivities(recorder)
    await ActivityEnvironment().run(activities.record_step, step_input("doing", note=""))
    await ActivityEnvironment().run(activities.record_step, step_input("done"))

    assert recorder.events == [
        EventRecord(
            TASK,
            "step_updated",
            {"step_id": "s1", "status": "doing", "note": "", "head_sha": SHA, "turn_index": 6},
        ),
        EventRecord(
            TASK,
            "step_updated",
            {
                "step_id": "s1",
                "status": "done",
                "note": "the tests of add() pass",
                "head_sha": SHA,
                "turn_index": 6,
            },
        ),
    ]


async def test_recording_a_step_again_leaves_one_checkpoint() -> None:
    recorder = MemoryRecorder()
    activities = PlanActivities(recorder)
    for _ in range(2):
        await ActivityEnvironment().run(activities.record_step, step_input("done"))
    assert len(recorder.checkpoints) == 1


async def test_two_steps_done_at_one_sha_each_get_a_checkpoint() -> None:
    recorder = MemoryRecorder()
    activities = PlanActivities(recorder)
    await ActivityEnvironment().run(activities.record_step, step_input("done", step_id="s1"))
    await ActivityEnvironment().run(activities.record_step, step_input("done", step_id="s2"))
    assert [c.step_id for c in recorder.checkpoints] == ["s1", "s2"]


def test_record_step_is_registered_with_the_activities_of_every_worker() -> None:
    names = [fn.__name__ for fn in PlanActivities(MemoryRecorder()).activities]
    assert names == ["store_plan", "record_step"]
