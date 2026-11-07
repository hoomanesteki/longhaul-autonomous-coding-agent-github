"""PR branch, pull request and issue labels, against FakeGitHub with a real git remote (LH-306).

GitHub is a FakeGitHub whose bare remote is a local repo, the way test_clone_push.py does it, so
the clone, the commits and the push are the real git commands against paths. The agent's work is
built without Docker: the task's bare repo is cloned from the remote, and two checkpoint commits
land on longhaul/42 as tool_exec would leave them. Nothing here needs a database. The ledger and
the recorder are the in-memory ones, which tests/integration/test_side_effects_table.py and
test_records.py hold to the same rules as the SQL ones.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tests.fakes.calc_remote import REPO, Github, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub, git

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import RepoSettings, TaskSpec
from longhaul.db.records import MemoryRecorder, ToolCallRecord
from longhaul.durable import acts
from longhaul.durable.activities.delivery import DeliveryActivities
from longhaul.github import lifecycle
from longhaul.github.side_effects import (
    MemoryLedger,
    add_label,
    effect_key,
    remove_label,
)
from longhaul.tools.output import store_output

pytestmark = [pytest.mark.integration, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
ISSUE = 42
SCRATCH = "longhaul/42"
PR_BRANCH = "longhaul-pr/42"
AUTHOR = "longhaul <longhaul@users.noreply.github.com>"
SUMMARY = "Fixed add() so it returns the sum of its arguments. All tests pass."
FIXED = (
    '"""Small arithmetic helpers."""\n\n\n'
    "def add(a: int, b: int) -> int:\n"
    '    """Return the sum of a and b."""\n'
    "    return a + b\n\n\n"
    "def multiply(a: int, b: int) -> int:\n"
    '    """Return the product of a and b."""\n'
    "    return a * b\n"
)
NEW_TEST = (
    "from calc import add\n\n\ndef test_add_negative() -> None:\n    assert add(-2, -3) == -5\n"
)
TEST_OUTPUT = "exit code: 0\nsummary: 4 passed in 0.12s\nstdout:\n....\n4 passed in 0.12s"


@dataclass
class World:
    task_id: str
    fake: FakeGitHub
    github: Github
    store: GitCheckpointStore
    blobs: MemoryBlobStore
    recorder: MemoryRecorder
    ledger: MemoryLedger
    activities: DeliveryActivities
    base_sha: str
    head_sha: str  # the last checkpoint of the scratch branch

    def spec(self) -> TaskSpec:
        return TaskSpec(
            task_id=self.task_id,
            repo=REPO,
            issue_number=ISSUE,
            title="add() subtracts",
            body="`calc.add` subtracts its arguments instead of adding them.",
            settings=RepoSettings(repo=REPO),
        )

    def bare(self, *args: str) -> str:
        return git(f"--git-dir={self.store.repo_path(self.task_id)}", *args)

    async def build(self, summary: str = SUMMARY) -> acts.PrBranchOutput:
        inp = acts.BuildPrBranchInput(
            task_id=self.task_id, repo=REPO, summary=summary, head_sha=self.head_sha
        )
        out: acts.PrBranchOutput = await ActivityEnvironment().run(
            self.activities.build_pr_branch, inp
        )
        return out

    async def deliver(self, built: acts.PrBranchOutput, summary: str = SUMMARY) -> str:
        inp = acts.DeliverInput(
            task_id=self.task_id,
            repo=REPO,
            issue_number=ISSUE,
            summary=summary,
            branch=built.branch,
            base_branch=built.base_branch,
            base_sha=built.base_sha,
            head_sha=built.head_sha,
            title=built.title,
        )
        url: str = await ActivityEnvironment().run(self.activities.deliver, inp)
        return url


def checkpoint(store: GitCheckpointStore, task_id: str, work: Path, files: dict[str, str]) -> str:
    """Commit `files` on top of the scratch branch the way tool_exec leaves a checkpoint."""
    bare = store.repo_path(task_id)
    if not work.exists():
        git("clone", "-q", str(bare), str(work))
        git("checkout", "-q", "-B", SCRATCH, f"origin/{SCRATCH}", cwd=work)
    for name, text in files.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    git("add", "-A", cwd=work)
    git("commit", "-q", "-m", f"lh: edit_file turn 1 call {uuid.uuid4().hex[:6]}", cwd=work)
    git("push", "-q", str(bare), f"HEAD:refs/heads/{SCRATCH}", cwd=work)
    return git("rev-parse", "HEAD", cwd=work)


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[World]:
    task_id = f"lh-deliver{uuid.uuid4().hex[:8]}-{ISSUE}"
    fake = FakeGitHub()
    base_sha = seed_calc(fake, tmp_path / "gh")
    github = github_for(fake)
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    await store.init_task(task_id, fake.clone_url(REPO), base_sha)
    # Two checkpoints, so that "one commit" is something the PR branch has to do on purpose.
    work = tmp_path / "work"
    checkpoint(store, task_id, work, {"src/calc/__init__.py": FIXED})
    head_sha = checkpoint(store, task_id, work, {"tests/test_negative.py": NEW_TEST})
    recorder = MemoryRecorder()
    output = await store_output(blobs, task_id, 3, "toolu_tests", TEST_OUTPUT)
    await recorder.record_tool_call(
        ToolCallRecord(
            id="toolu_tests",
            task_id=task_id,
            turn_index=3,
            name="run_tests",
            ok=True,
            exit_code=0,
            output_ref=output.key,
        )
    )
    ledger = MemoryLedger()
    activities = DeliveryActivities(
        store,
        github.remote,
        github.client,
        ledger,
        recorder,
        blobs,
        author=AUTHOR,
        templates=ROOT / "templates",
    )
    try:
        yield World(
            task_id, fake, github, store, blobs, recorder, ledger, activities, base_sha, head_sha
        )
    finally:
        await github.aclose()


async def test_pr_branch_single_commit_equals_scratch(world: World) -> None:
    built = await world.build()

    assert built.branch == PR_BRANCH
    # Exactly one commit on top of base, though the agent made two.
    commits = world.bare("rev-list", f"{world.base_sha}..{built.head_sha}").splitlines()
    assert commits == [built.head_sha]
    assert world.bare("rev-parse", f"{built.head_sha}^") == world.base_sha
    assert world.bare("rev-parse", f"refs/heads/{PR_BRANCH}") == built.head_sha
    # Its change is the change of the scratch head, to the byte.
    pr_diff = await world.store.diff(world.task_id, built.head_sha)
    scratch_diff = await world.store.diff(world.task_id, world.head_sha)
    assert pr_diff == scratch_diff != ""
    assert world.bare("rev-parse", f"{built.head_sha}^{{tree}}") == world.bare(
        "rev-parse", f"{world.head_sha}^{{tree}}"
    )
    # Written in the style guide, by the configured author, and never on the default branch.
    assert built.title == "fix(calc): fix add() so it returns the sum of its arguments"
    assert world.bare("log", "-1", "--format=%s", built.head_sha) == built.title
    assert world.bare("log", "-1", "--format=%an <%ae>", built.head_sha) == AUTHOR
    assert world.bare("log", "-1", "--format=%cn <%ce>", built.head_sha) == AUTHOR
    assert built.base_sha == world.base_sha
    assert built.base_branch == "main"
    assert world.fake.branches(REPO) == {"main": world.base_sha}, "building pushes nothing"


async def test_the_pr_branch_is_the_same_commit_when_built_again(world: World) -> None:
    # A retried activity must make the commit it made the first time. A different sha would be
    # refused by the push after the first attempt had already pushed.
    first = await world.build()
    second = await world.build()

    assert first == second


async def test_deliver_pushes_the_branch_and_opens_a_draft_pull_request(world: World) -> None:
    built = await world.build()

    url = await world.deliver(built)

    assert world.fake.branches(REPO)[PR_BRANCH] == built.head_sha
    assert world.fake.branches(REPO)["main"] == world.base_sha
    (pull,) = world.fake.pulls(REPO)
    assert pull["html_url"] == url
    assert pull["draft"] is True
    assert pull["title"] == built.title
    assert pull["head"]["ref"] == PR_BRANCH
    assert pull["base"]["ref"] == "main"
    # The push is on the ledger next to the pull request.
    assert world.ledger.rows[effect_key(world.task_id, "push", 1)].external_id == built.head_sha
    assert world.ledger.rows[effect_key(world.task_id, "pr", 1)].external_id == url


async def test_pr_body_fields(world: World) -> None:
    built = await world.build()

    await world.deliver(built)

    (pull,) = world.fake.pulls(REPO)
    body = pull["body"]
    assert f"Closes #{ISSUE}" in body
    assert "4 passed" in body, "the summary of the last run_tests output"
    assert "exit code 0" in body
    assert "Fixed add() so it returns the sum" in body
    # The changes per file, from the diff stat of the branch.
    assert re.search(r"`src/calc/__init__\.py`.*\+1.*-1", body)
    assert "`tests/test_negative.py`" in body
    assert body.rstrip().endswith(f"Closes #{ISSUE}")


async def test_deliver_twice_changes_nothing(world: World) -> None:
    built = await world.build()

    first = await world.deliver(built)
    second = await world.deliver(built)

    assert first == second
    assert world.fake.count_pulls(REPO) == 1
    assert world.fake.branches(REPO)[PR_BRANCH] == built.head_sha


async def test_deliver_after_the_ledger_is_lost_finds_the_pull_request(world: World) -> None:
    built = await world.build()
    first = await world.deliver(built)
    world.ledger.rows.clear()  # the database was lost, GitHub still has the branch and the PR

    assert await world.deliver(built) == first
    assert world.fake.count_pulls(REPO) == 1


async def test_a_task_without_a_test_run_says_so(world: World) -> None:
    world.recorder.tool_calls.clear()
    built = await world.build()

    await world.deliver(built)

    (pull,) = world.fake.pulls(REPO)
    assert "No test run was recorded" in pull["body"]
    assert f"Closes #{ISSUE}" in pull["body"]


async def test_a_task_that_changed_nothing_has_nothing_to_deliver(world: World) -> None:
    inp = acts.BuildPrBranchInput(
        task_id=world.task_id,
        repo=REPO,
        summary="Looked and found it fine.",
        head_sha=world.base_sha,
    )

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(world.activities.build_pr_branch, inp)

    assert caught.value.type == acts.NOTHING_TO_DELIVER
    assert caught.value.non_retryable
    assert world.fake.branches(REPO) == {"main": world.base_sha}


async def test_a_pull_request_call_that_fails_is_retried_without_a_second_pull_request(
    world: World,
) -> None:
    built = await world.build()
    world.fake.after_write_fail(r"/pulls$")  # the pull request is made, the answer is lost

    with pytest.raises(ApplicationError) as caught:
        await world.deliver(built)
    assert caught.value.type == "GitHubRetryable"
    assert not caught.value.non_retryable

    url = await world.deliver(built)  # the next attempt of Temporal
    assert world.fake.count_pulls(REPO) == 1
    assert world.fake.pulls(REPO)[0]["html_url"] == url


async def test_a_push_to_the_default_branch_is_refused_and_recorded(world: World) -> None:
    built = await world.build()
    before = world.fake.refs(REPO)
    inp = acts.DeliverInput(
        task_id=world.task_id,
        repo=REPO,
        issue_number=ISSUE,
        summary=SUMMARY,
        branch="main",
        base_branch="main",
        base_sha=built.base_sha,
        head_sha=built.head_sha,
        title=built.title,
    )

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(world.activities.deliver, inp)

    assert caught.value.type == "ForbiddenBranchError"
    assert caught.value.non_retryable
    assert world.fake.refs(REPO) == before
    assert world.fake.count_pulls(REPO) == 0
    (event,) = world.recorder.events
    assert (event.task_id, event.type) == (world.task_id, "branch_refused")
    assert event.payload["branch"] == "main"


async def test_a_test_log_that_is_gone_does_not_stop_the_delivery(world: World) -> None:
    row = world.recorder.tool_calls["toolu_tests"]
    world.recorder.tool_calls["toolu_tests"] = replace(row, output_ref="tasks/x/gone.txt")
    built = await world.build()

    await world.deliver(built)

    (pull,) = world.fake.pulls(REPO)
    assert "no pytest summary line" in pull["body"]
    assert f"Closes #{ISSUE}" in pull["body"]


async def test_a_test_call_that_never_ran_the_tests_is_described_as_such(world: World) -> None:
    row = world.recorder.tool_calls["toolu_tests"]
    world.recorder.tool_calls["toolu_tests"] = replace(row, ok=False, exit_code=None)
    built = await world.build()

    await world.deliver(built)

    (pull,) = world.fake.pulls(REPO)
    assert "could not run its tests" in pull["body"]


async def test_the_summary_of_the_model_cannot_close_another_issue(world: World) -> None:
    summary = "Fixed add(). This also closes #7, thanks @octocat."
    built = await world.build(summary)

    await world.deliver(built, summary)

    (pull,) = world.fake.pulls(REPO)
    assert re.findall(r"(?i)\bcloses\s+#\d+", pull["body"]) == [f"Closes #{ISSUE}"]
    assert "@octocat" not in pull["body"]


# ---------- the labels of the issue ----------
async def apply(world: World, steps: list[lifecycle.LabelStep]) -> None:
    """Run the steps of one transition with the plain functions, as the activities would."""
    for step in steps:
        run = add_label if step.add else remove_label
        await run(world.github.client, world.ledger, step.label)


def labels(world: World) -> list[str]:
    return sorted(world.fake.label_names(REPO, ISSUE))


async def test_label_transitions(world: World) -> None:
    spec = world.spec()
    world.fake.seed_issue(REPO, "add() subtracts", labels=["longhaul"], number=ISSUE)
    assert labels(world) == ["longhaul"]

    await apply(world, lifecycle.on_pickup(spec))
    assert labels(world) == ["longhaul", "longhaul:working"]

    await apply(world, lifecycle.on_pr_opened(spec))
    assert labels(world) == ["longhaul", "longhaul:pr-open"]

    await apply(world, lifecycle.on_pause(spec, 1))
    assert labels(world) == ["longhaul", "longhaul:needs-human", "longhaul:pr-open"]

    await apply(world, lifecycle.on_resume(spec, 1))
    assert labels(world) == ["longhaul", "longhaul:pr-open"]

    # The audit trail has each change once, in order.
    changes = [(e.action, e.label) for e in world.fake.label_events(REPO, ISSUE)]
    assert changes == [
        ("labeled", "longhaul:working"),
        ("unlabeled", "longhaul:working"),
        ("labeled", "longhaul:pr-open"),
        ("labeled", "longhaul:needs-human"),
        ("unlabeled", "longhaul:needs-human"),
    ]


async def test_a_transition_run_again_changes_nothing(world: World) -> None:
    spec = world.spec()
    world.fake.seed_issue(REPO, "add() subtracts", labels=["longhaul"], number=ISSUE)

    for _ in range(2):
        await apply(world, lifecycle.on_pickup(spec))
        await apply(world, lifecycle.on_pr_opened(spec))

    assert labels(world) == ["longhaul", "longhaul:pr-open"]
    assert len(world.fake.label_events(REPO, ISSUE)) == 3


async def test_a_second_pause_is_a_second_effect(world: World) -> None:
    # Keys are one per effect, so a task that pauses twice needs a fresh key for each pause.
    spec = world.spec()
    world.fake.seed_issue(REPO, "add() subtracts", labels=["longhaul"], number=ISSUE)

    await apply(world, lifecycle.on_pause(spec, 1))
    await apply(world, lifecycle.on_resume(spec, 1))
    await apply(world, lifecycle.on_pause(spec, 2))

    assert labels(world) == ["longhaul", "longhaul:needs-human"]
