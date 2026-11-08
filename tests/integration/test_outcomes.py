"""How a task ends: one outcome, one report, no leftovers (LH-507).

Whatever way a task ends, TaskWorkflow sets the Outcome, posts one final comment for a task of an
issue, sets the final labels, destroys the sandbox and leaves the bare repo to the janitor. The
tests run the workflow on a time skipping Temporal test server. The first group uses the real
llm_turn, tool_exec, summarize_for_human, record_event and GitHub write activities, a fake sandbox
and a FakeGitHub, so the comment, its key and the labels can be read back. The delivery
activities are stubs that answer with a branch and a URL. The second group runs the real
prepare_workspace and cleanup on Docker and looks for containers and volumes afterwards.

A task that raises ends with outcome error and still fails the workflow, as it did before: a
failed workflow is what the Temporal UI and the retry command show. The outcome is in status(), in
the final comment and in the `outcome` event.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker
import pytest
from docker.errors import NotFound
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from tests.fakes.calc_remote import Github, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub
from tests.integration.test_pause_flow import (
    BASE,
    CFG,
    CONTEXT,
    FINISH,
    PRICES,
    REPO,
    SUMMARY,
    SUMMARY_STEP,
    TEMPLATES,
    Boxes,
    Probe,
    loop_then_finish,
    paused,
    result_of,
    tool,
    turn,
    write_script,
)

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.checkpoints.squash import FileStat
from longhaul.contracts import (
    BlobRef,
    Budgets,
    Outcome,
    OutcomeKind,
    Phase,
    RepoSettings,
    Role,
    SandboxLimits,
    TaskSpec,
)
from longhaul.cost.ledger import MemoryLedger as CostLedger
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.context import ContextActivities
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.outcome import FinalCommentActivities
from longhaul.durable.activities.plan import PlanActivities, StepActivities
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.durable.client import start_task
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.poller import PollerWorkflow
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github import lifecycle
from longhaul.github.side_effects import MemoryLedger, SideEffectActivities
from longhaul.providers.scripted import ScriptedProvider
from longhaul.roles import coder, planner
from longhaul.sandbox.docker_manager import TASK_LABEL, DockerSandboxManager, volume_name
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase5]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
HISTORIES = ROOT / "tests" / "fixtures" / "histories"
IMAGE = "longhaul-sandbox:py312"
DOCKER_WAIT_S = 300.0
PR_URL = f"https://github.com/{REPO}/pull/7"
KINDS = ["pr_opened", "stuck", "cancelled", "error"]
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=3)


# ---------- the stubs: delivery, the sandbox and the diff ----------
class Diffs:
    """The diff reader of the final comment: a fixed list of files, and what it was asked."""

    def __init__(self, files: list[FileStat]) -> None:
        self.files = files
        self.asked: list[tuple[str, str, str]] = []

    async def stats(self, task_id: str, base_sha: str, head_sha: str) -> list[FileStat]:
        self.asked.append((task_id, base_sha, head_sha))
        return self.files


class Stubs(Boxes):
    """The sandbox of test_pause_flow, plus a cleanup that is counted and a stub delivery."""

    def __init__(self) -> None:
        super().__init__()
        self.cleanups: list[acts.CleanupInput] = []
        self.branches: list[acts.BuildPrBranchInput] = []
        self.deliveries: list[acts.DeliverInput] = []
        self.build_error: ApplicationError | None = None
        # LH-605: a worker with memory loads a block at the start and asks for lessons at the end.
        # Each ask is kept with the number of draft pull requests opened before it.
        self.with_memory = False
        self.blobs: MemoryBlobStore | None = None
        self.lessons: list[tuple[int, acts.ExtractLessonsInput]] = []

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        prepared = await super().prepare_workspace(spec)
        return prepared.model_copy(update={"memory_enabled": self.with_memory})

    @activity.defn(name=acts.LOAD_MEMORY.name)
    async def load_memory(self, inp: acts.LoadMemoryInput) -> BlobRef:
        assert self.blobs is not None
        key = f"tasks/{inp.spec.task_id}/memory/0000/memory-block.txt"
        return await self.blobs.put(b"", key=key)

    @activity.defn(name=acts.EXTRACT_LESSONS.name)
    async def extract_lessons(self, inp: acts.ExtractLessonsInput) -> acts.ExtractLessonsOutput:
        self.lessons.append((len(self.deliveries), inp))
        return acts.ExtractLessonsOutput(status="skipped", source_ref="")

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        self.cleanups.append(inp)

    @activity.defn(name=acts.BUILD_PR_BRANCH.name)
    async def build_pr_branch(self, inp: acts.BuildPrBranchInput) -> acts.PrBranchOutput:
        self.branches.append(inp)
        if self.build_error is not None:
            raise self.build_error
        return acts.PrBranchOutput(
            branch="longhaul-pr/1",
            head_sha=inp.head_sha,
            base_sha=inp.base_sha,
            base_branch="main",
            title="fix(calc): fix the lookup",
        )

    @activity.defn(name=acts.DELIVER.name)
    async def deliver(self, inp: acts.DeliverInput) -> str:
        self.deliveries.append(inp)
        return PR_URL


class Flaky(FinalCommentActivities):
    """post_final that posts, and then reports a failure once, as a 502 that landed does."""

    posts = 0

    @activity.defn(name=acts.POST_FINAL.name)
    async def post_final(self, inp: acts.PostFinalInput) -> str:
        type(self).posts += 1
        comment_id = await super().post_final(inp)
        if type(self).posts == 1:
            raise ApplicationError("GitHub answered 502", type=acts.GITHUB_RETRYABLE)
        return comment_id


class Refusing(FinalCommentActivities):
    """post_final that GitHub refuses for good."""

    @activity.defn(name=acts.POST_FINAL.name)
    async def post_final(self, inp: acts.PostFinalInput) -> str:
        raise ApplicationError(
            "Resource not accessible", type=acts.GITHUB_FATAL, non_retryable=True
        )


@dataclass
class Rig:
    queue: str
    provider: Probe
    recorder: MemoryRecorder
    fake: FakeGitHub
    number: int
    stubs: Stubs
    diffs: Diffs

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.recorder.events if e.type == kind]

    def spec(self, *, local: bool = False) -> TaskSpec:
        if local:
            repo = "local:/work/inv"
            return TaskSpec(
                task_id=f"lh-local-inv-{uuid.uuid4().hex[:8]}",
                repo=repo,
                issue_number=None,
                title="lookup fails",
                body="`inv.lookup` raises KeyError.",
                settings=RepoSettings(repo=repo, budgets=Budgets()),
            )
        return TaskSpec(
            task_id=f"lh-out{uuid.uuid4().hex[:8]}-{self.number}",
            repo=REPO,
            issue_number=self.number,
            title="lookup fails",
            body="`inv.lookup` raises KeyError for a sku that exists.",
            settings=RepoSettings(repo=REPO, budgets=Budgets()),
        )

    def final_comments(self) -> int:
        return self.fake.count_comments(REPO, self.number, containing=":final:1 -->")

    def final_comment(self) -> str:
        (body,) = [b for b in self.fake.comment_bodies(REPO, self.number) if ":final:1 -->" in b]
        return body

    def labels(self) -> set[str]:
        return set(self.fake.label_names(REPO, self.number))


@asynccontextmanager
async def rig(
    env: WorkflowEnvironment,
    steps: list[dict[str, Any]],
    tmp_path: Path,
    *,
    posting: type[FinalCommentActivities] = FinalCommentActivities,
    files: list[FileStat] | None = None,
) -> AsyncIterator[Rig]:
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    costs = CostLedger(PRICES)
    provider = Probe(write_script(tmp_path, steps))
    registry = default_registry(test_command="true", blobs=blobs)

    def config_for(role: Role) -> Any:
        return CFG

    llm = LlmActivities(
        provider,
        blobs,
        recorder,
        registry.specs(),
        config_for,
        ledger=costs,
        role_tools={Role.PLANNER: planner.tool_specs(registry.specs())},
        planned_tools=coder.tool_specs(registry.specs()),
    )
    questions = ContextActivities(
        provider,
        blobs,
        registry.specs(),
        config_for,
        CONTEXT,
        ledger=costs,
        templates=TEMPLATES,
    )
    stubs = Stubs()
    stubs.blobs = blobs
    tools = ToolActivities(registry, stubs, blobs, recorder, checkpoints=NoCheckpoints())
    fake = FakeGitHub()
    seed_calc(fake, tmp_path / "gh")
    number = fake.seed_issue(REPO, "lookup fails")
    github: Github = github_for(fake)
    ledger = MemoryLedger()
    diffs = Diffs(files if files is not None else [FileStat("inv.py", 3, 1)])
    final = posting(github.client, ledger, diffs, templates=TEMPLATES)
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *questions.activities,
                *tools.activities,
                *EventActivities(recorder).activities,
                *PlanActivities(recorder).activities,
                *StepActivities(recorder).activities,
                *SideEffectActivities(github.client, ledger).activities,
                *final.activities,
                stubs.prepare_workspace,
                stubs.cleanup,
                stubs.ensure_sandbox,
                stubs.bundle_checkpoint,
                stubs.build_pr_branch,
                stubs.deliver,
                stubs.load_memory,
                stubs.extract_lessons,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield Rig(queue, provider, recorder, fake, number, stubs, diffs)
    finally:
        await stubs.destroy_all()
        await github.aclose()


DELIVERS = [turn("coder", 0, FINISH), SUMMARY_STEP]
FAILS = loop_then_finish(15)


# ---------- each outcome reports once ----------
@dataclass(frozen=True)
class Ending:
    steps: list[dict[str, Any]]
    kind: OutcomeKind
    heading: str
    labels: set[str]


ENDINGS = {
    "pr_opened": Ending(
        DELIVERS,
        OutcomeKind.PR_OPENED,
        "## Longhaul opened a draft pull request",
        {lifecycle.PR_OPEN},
    ),
    "stuck": Ending(
        FAILS, OutcomeKind.STUCK, "## Longhaul is stuck and has stopped", {lifecycle.NEEDS_HUMAN}
    ),
    "cancelled": Ending(
        FAILS,
        OutcomeKind.CANCELLED,
        "## Longhaul stopped: the task was cancelled",
        set(),
    ),
    "error": Ending(
        DELIVERS, OutcomeKind.ERROR, "## Longhaul hit an error and stopped", {lifecycle.NEEDS_HUMAN}
    ),
}


async def finish_the_run(world: Rig, handle: Any, kind: str) -> None:
    """Wait for the task to end the way `kind` says. For `error` the workflow fails."""
    if kind == "error":
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)
    elif kind == "cancelled":
        await paused(world, handle)
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "not worth it"])
        await result_of(handle)
    else:
        await result_of(handle)  # for stuck, the test server skips the 24 hours


async def end_the_task(
    env: WorkflowEnvironment, world: Rig, spec: TaskSpec, kind: str, **start: Any
) -> Outcome:
    """Run the task to the end that `kind` names and return the outcome that status() shows."""
    if kind == "error":
        world.stubs.build_error = ApplicationError(
            "push rejected", type="CheckpointError", non_retryable=True
        )
    handle = await start_task(env.client, spec, task_queue=world.queue, **start)
    await finish_the_run(world, handle, kind)
    status = await handle.query(TaskWorkflow.status)
    assert status.outcome is not None
    return status.outcome


@pytest.mark.parametrize("kind", KINDS)
async def test_each_outcome_reports_once(
    env: WorkflowEnvironment, tmp_path: Path, kind: str
) -> None:
    ending = ENDINGS[kind]
    async with rig(env, ending.steps, tmp_path) as world:
        spec = world.spec()
        outcome = await end_the_task(env, world, spec, kind)

        assert outcome.kind is ending.kind
        assert world.final_comments() == 1
        assert world.final_comment().startswith(ending.heading)
        assert world.labels() == ending.labels
        # The sandbox is gone once. The repo is left to the janitor, unless a person cancelled.
        (cleaned,) = world.stubs.cleanups
        assert cleaned.task_id == spec.task_id and cleaned.keep_repo is (kind != "cancelled")
        assert cleaned.reason == (
            "not worth it" if kind == "cancelled" else f"the task ended: {kind}"
        )
        assert cleaned.sandbox_id == f"sbx-{spec.task_id}"
        # The note the metrics count from is made once.
        assert [e["kind"] for e in world.events("outcome")] == [ending.kind.value]


async def test_a_delivered_task_links_its_pull_request_in_the_comment(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, DELIVERS, tmp_path) as world:
        outcome = await end_the_task(env, world, world.spec(), "pr_opened")

        assert outcome.pr_url == PR_URL
        assert PR_URL in world.final_comment()
        assert SUMMARY in world.final_comment()


async def test_a_stuck_report_says_what_was_done_and_left_and_the_diff(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path) as world:
        spec = world.spec()
        await end_the_task(env, world, spec, "stuck")

        body = world.final_comment()
        assert "no hint in 24 hours" in body
        assert "1 file changed, 3 additions, 1 deletion" in body
        assert "- `inv.py`: +3 -1" in body
        assert world.diffs.asked == [(spec.task_id, BASE, BASE)]


async def test_a_cancelled_task_names_who_cancelled_it(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path) as world:
        await end_the_task(env, world, world.spec(), "cancelled")

        assert "cancelled by maintainer: not worth it" in world.final_comment()


async def test_a_cancelled_task_that_was_paused_lets_go_of_needs_human(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path) as world:
        spec = world.spec()
        handle = await start_task(env.client, spec, task_queue=world.queue)
        await paused(world, handle)
        assert lifecycle.NEEDS_HUMAN in world.labels()
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "enough"])
        await result_of(handle)

        assert world.labels() == set()


# ---------- the workflow state, not the activities, keeps a step from running twice ----------
async def test_a_comment_that_landed_but_looked_like_a_failure_is_posted_once(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    Flaky.posts = 0
    async with rig(env, FAILS, tmp_path, posting=Flaky) as world:
        await end_the_task(env, world, world.spec(), "stuck")

        assert Flaky.posts == 2, "the activity ran again"
        assert world.final_comments() == 1
        assert len(world.stubs.cleanups) == 1


async def test_a_report_that_cannot_be_posted_does_not_change_the_outcome(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path, posting=Refusing) as world:
        outcome = await end_the_task(env, world, world.spec(), "stuck")

        assert outcome.kind is OutcomeKind.STUCK
        assert world.final_comments() == 0
        # Everything after the comment still happened.
        assert world.labels() == {lifecycle.NEEDS_HUMAN}
        assert len(world.stubs.cleanups) == 1
        assert [e["kind"] for e in world.events("outcome")] == ["stuck"]


async def test_the_sandbox_is_destroyed_before_github_is_asked_for_anything(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    # GitHub can be down for twenty minutes. The container must not wait for it.
    async with rig(env, FAILS, tmp_path, posting=Refusing) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await result_of(handle)
        history = await handle.fetch_history()

    names = [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.HasField("activity_task_scheduled_event_attributes")
    ]
    assert names.index(acts.CLEANUP.name) < names.index(acts.POST_FINAL.name)


# ---------- a task that is not of an issue ----------
async def test_a_local_task_is_cleaned_up_and_posts_no_comment(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, DELIVERS, tmp_path) as world:
        spec = world.spec(local=True)
        handle = await start_task(env.client, spec, task_queue=world.queue)
        outcome = await result_of(handle)

        assert outcome.kind is OutcomeKind.PR_OPENED
        (cleaned,) = world.stubs.cleanups
        assert cleaned.sandbox_id == f"sbx-{spec.task_id}" and cleaned.keep_repo is True
        assert world.diffs.asked == []
        assert world.fake.count_comments(REPO) == 0
        assert [e["kind"] for e in world.events("outcome")] == ["pr_opened"]


# ---------- unexpected errors ----------
async def test_an_error_is_reported_with_a_short_summary_and_the_trace_goes_to_the_log(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, DELIVERS, tmp_path) as world:
        world.stubs.build_error = ApplicationError(
            "push rejected: " + "x" * 60_000, type="CheckpointError", non_retryable=True
        )
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

        assert status.phase is Phase.FAILED
        assert status.outcome == Outcome(
            kind=OutcomeKind.ERROR, summary="workflow failed: CheckpointError"
        )
        assert "x" * 100 not in world.final_comment(), "the summary does not echo the message"
        (logged,) = world.events("error")
        assert logged["type"] == "CheckpointError"
        assert "push rejected" in logged["trace"]
        assert len(logged["trace"]) <= acts.ERROR_TRACE_CHARS


async def test_a_bug_in_the_workflow_is_an_error_outcome_and_not_a_loop(
    env: WorkflowEnvironment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any) -> Any:
        raise RuntimeError("a bug in the label rules")

    async with rig(env, DELIVERS, tmp_path) as world:
        monkeypatch.setattr(lifecycle, "on_pickup", broken)
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        with pytest.raises(WorkflowFailureError):
            await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

        assert status.outcome == Outcome(
            kind=OutcomeKind.ERROR, summary="unexpected error: RuntimeError"
        )
        assert world.final_comments() == 1
        assert world.final_comment().startswith("## Longhaul hit an error and stopped")
        (logged,) = world.events("error")
        assert "RuntimeError: a bug in the label rules" in logged["trace"]
        assert len(world.stubs.cleanups) == 1


# ---------- a draft pull request for partial work ----------
async def test_a_stuck_task_opens_a_draft_when_the_repo_asks_for_one(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path) as world:
        spec = world.spec()
        outcome = await end_the_task(env, world, spec, "stuck", open_draft_on_stuck=True)

        assert outcome.kind is OutcomeKind.STUCK and outcome.pr_url == PR_URL
        (delivery,) = world.stubs.deliveries
        assert delivery.issue_number == world.number
        assert delivery.summary.startswith("Partial work. The task is stuck:")
        body = world.final_comment()
        assert PR_URL in body and "draft pull request" in body
        assert world.labels() == {lifecycle.NEEDS_HUMAN, lifecycle.PR_OPEN}


async def test_the_lessons_of_a_stuck_task_are_asked_after_its_draft_is_open(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path) as world:
        world.stubs.with_memory = True
        outcome = await end_the_task(env, world, world.spec(), "stuck", open_draft_on_stuck=True)

        # The pull request number is the source of the candidates, so the draft comes first.
        ((drafts, asked),) = world.stubs.lessons
        assert outcome.pr_url == PR_URL
        assert drafts == 1
        assert asked.outcome.pr_url == PR_URL


async def test_a_stuck_task_opens_no_draft_unless_asked(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path) as world:
        outcome = await end_the_task(env, world, world.spec(), "stuck")

        assert outcome.pr_url is None
        assert world.stubs.branches == [] and world.stubs.deliveries == []


async def test_a_stuck_task_with_no_changed_file_opens_no_draft(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path) as world:
        world.stubs.build_error = ApplicationError(
            "the work changes no file", type=acts.NOTHING_TO_DELIVER, non_retryable=True
        )
        outcome = await end_the_task(env, world, world.spec(), "stuck", open_draft_on_stuck=True)

        assert outcome.kind is OutcomeKind.STUCK and outcome.pr_url is None
        assert len(world.stubs.branches) == 1 and world.stubs.deliveries == []
        assert world.labels() == {lifecycle.NEEDS_HUMAN}
        assert world.final_comments() == 1


async def test_a_draft_that_cannot_be_opened_does_not_stop_the_report(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with rig(env, FAILS, tmp_path) as world:
        world.stubs.build_error = ApplicationError(
            "push rejected", type="CheckpointError", non_retryable=True
        )
        outcome = await end_the_task(env, world, world.spec(), "stuck", open_draft_on_stuck=True)

        assert outcome.kind is OutcomeKind.STUCK and outcome.pr_url is None
        assert world.final_comments() == 1
        assert len(world.stubs.cleanups) == 1


# ---------- replay ----------
def replayer() -> Replayer:
    return Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=guarded_data_converter,
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_the_history_of_each_ending_replays(
    env: WorkflowEnvironment, tmp_path: Path, kind: str
) -> None:
    async with rig(env, ENDINGS[kind].steps, tmp_path) as world:
        spec = world.spec()
        if kind == "error":
            world.stubs.build_error = ApplicationError(
                "push rejected", type="CheckpointError", non_retryable=True
            )
        handle = await start_task(env.client, spec, task_queue=world.queue)
        await finish_the_run(world, handle, kind)
        history = await handle.fetch_history()

    await replayer().replay_workflow(history)


@pytest.mark.parametrize(
    "name", ["stuck_unanswered", "cancelled_while_paused", "finished_after_hint"]
)
async def test_histories_from_before_this_story_replay(name: str) -> None:
    """A run that began before LH-507 keeps the end it recorded (F13).

    The three files were recorded with the code of the commit before this story. They end without
    a cleanup of the sandbox (a finished task) or with the cleanup of a cancel, and the new code
    must make those commands and no others at the same places.
    """
    path = HISTORIES / f"{name}.json"
    history = WorkflowHistory.from_json(f"replay-{name}", path.read_text(encoding="utf-8"))
    await replayer().replay_workflow(history)


# ---------- the janitor runs on the poller schedule ----------
class PollStubs:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.poll_error: ApplicationError | None = None
        self.sweep_error: ApplicationError | None = None

    @activity.defn(name=acts.POLL_ONCE.name)
    async def poll_once(self) -> acts.PollResult:
        self.calls.append("poll_once")
        if self.poll_error is not None:
            raise self.poll_error
        return acts.PollResult(repos=1, issues=0, started=0, known=0)

    @activity.defn(name=acts.CLEAN_REPOS.name)
    async def clean_repos(self) -> acts.JanitorResult:
        self.calls.append("clean_repos")
        if self.sweep_error is not None:
            raise self.sweep_error
        return acts.JanitorResult(removed=[], kept=0, failed=0)


@asynccontextmanager
async def polling(env: WorkflowEnvironment, stubs: PollStubs) -> AsyncIterator[str]:
    queue = f"lh-poll-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[PollerWorkflow],
        activities=[stubs.poll_once, stubs.clean_repos],
        workflow_runner=workflow_runner(),
    ):
        yield queue


async def run_poller(env: WorkflowEnvironment, queue: str) -> acts.PollResult:
    result: acts.PollResult = await env.client.execute_workflow(
        PollerWorkflow.run, id=f"poll-{uuid.uuid4().hex[:8]}", task_queue=queue
    )
    return result


async def test_the_poller_sweeps_old_repos_after_each_poll(env: WorkflowEnvironment) -> None:
    stubs = PollStubs()
    async with polling(env, stubs) as queue:
        result = await run_poller(env, queue)

    assert result == acts.PollResult(repos=1, issues=0, started=0, known=0)
    assert stubs.calls == ["poll_once", "clean_repos"]


async def test_a_sweep_that_fails_does_not_fail_the_poll(env: WorkflowEnvironment) -> None:
    stubs = PollStubs()
    stubs.sweep_error = ApplicationError("disk error", non_retryable=True)
    async with polling(env, stubs) as queue:
        result = await run_poller(env, queue)

    assert result.repos == 1
    assert stubs.calls == ["poll_once", "clean_repos"]


async def test_a_poll_that_fails_still_sweeps(env: WorkflowEnvironment) -> None:
    stubs = PollStubs()
    stubs.poll_error = ApplicationError("bad config", type=acts.POLLER_CONFIG, non_retryable=True)
    async with polling(env, stubs) as queue:
        with pytest.raises(WorkflowFailureError):
            await run_poller(env, queue)

    assert stubs.calls == ["poll_once", "clean_repos"]


# ---------- real Docker: no sandbox is left behind ----------
class Gate(ScriptedProvider):
    """A scripted provider that holds the model call of one turn until the test cancels."""

    def __init__(self, script: Path, *, hold_turn: int | None = None) -> None:
        super().__init__(script)
        self.hold_turn = hold_turn
        self.held = asyncio.Event()

    async def complete(self, **kwargs: Any) -> Any:
        turns = sum(1 for m in kwargs["messages"] if m.role == "assistant")
        if self.hold_turn is not None and turns == self.hold_turn:
            self.held.set()
            await asyncio.Event().wait()
        return await super().complete(**kwargs)


@dataclass
class Machine:
    """The pieces of a worker on real Docker for one task, and the task's folder."""

    task_id: str
    source: Path
    manager: DockerSandboxManager
    store: GitCheckpointStore
    blobs: MemoryBlobStore
    recorder: MemoryRecorder
    queue: str = field(default_factory=lambda: f"lh-test-{uuid.uuid4().hex[:8]}")

    def spec(self, turns: int = 20) -> TaskSpec:
        repo = f"local:{self.source}"
        return TaskSpec(
            task_id=self.task_id,
            repo=repo,
            issue_number=42,
            title="lookup fails",
            body="`calc.add` subtracts its arguments instead of adding them.",
            # An empty setup command means none: the image is enough for these scripts.
            settings=RepoSettings(repo=repo, setup_command="", budgets=Budgets(task_turns=turns)),
        )

    def containers(self) -> list[Any]:
        return docker.from_env().containers.list(
            all=True, filters={"label": f"{TASK_LABEL}={self.task_id}"}
        )

    def volume_exists(self) -> bool:
        try:
            docker.from_env().volumes.get(volume_name(self.task_id))
        except NotFound:
            return False
        return True


@pytest.fixture
async def machine(tmp_path: Path) -> AsyncIterator[Machine]:
    task_id = f"lh-out{uuid.uuid4().hex[:8]}-42"
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    manager = DockerSandboxManager()
    built = Machine(
        task_id, shutil.copytree(CALC, tmp_path / "calc"), manager, store, blobs, MemoryRecorder()
    )
    try:
        yield built
    finally:
        await manager.cleanup_task(task_id)
        await store.cleanup_task(task_id)


@asynccontextmanager
async def on_docker(
    env: WorkflowEnvironment, machine: Machine, provider: ScriptedProvider
) -> AsyncIterator[str]:
    registry = default_registry(test_command="true", blobs=machine.blobs)
    costs = CostLedger(PRICES)

    def config_for(role: Role) -> Any:
        return CFG

    llm = LlmActivities(
        provider,
        machine.blobs,
        machine.recorder,
        registry.specs(),
        config_for,
        ledger=costs,
        role_tools={Role.PLANNER: planner.tool_specs(registry.specs())},
        planned_tools=coder.tool_specs(registry.specs()),
    )
    questions = ContextActivities(
        provider,
        machine.blobs,
        registry.specs(),
        config_for,
        CONTEXT,
        ledger=costs,
        templates=TEMPLATES,
    )
    tools = ToolActivities(
        registry, machine.manager, machine.blobs, machine.recorder, checkpoints=machine.store
    )
    workspace = WorkspaceActivities(
        machine.manager, machine.store, machine.recorder, image=IMAGE, limits=SandboxLimits()
    )
    async with Worker(
        env.client,
        task_queue=machine.queue,
        workflows=[TaskWorkflow],
        activities=[
            *llm.activities,
            *questions.activities,
            *tools.activities,
            *workspace.activities,
            *EventActivities(machine.recorder).activities,
        ],
        workflow_runner=workflow_runner(),
    ):
        yield machine.queue


LIST_THEN_FINISH = [
    turn("coder", 0, tool("list_dir", path=".")),
    turn("coder", 1, FINISH),
]


@pytest.mark.parametrize("kind", ["pr_opened", "error", "cancelled", "stuck"])
async def test_no_leftover_containers(
    env: WorkflowEnvironment, machine: Machine, tmp_path: Path, kind: str
) -> None:
    steps = {
        "pr_opened": LIST_THEN_FINISH,
        "error": LIST_THEN_FINISH,  # one turn is allowed, so the second ends the run
        "cancelled": LIST_THEN_FINISH,
        "stuck": loop_then_finish(3),
    }[kind]
    provider = Gate(write_script(tmp_path, steps), hold_turn=1 if kind == "cancelled" else None)
    spec = machine.spec(turns=1 if kind == "error" else 20)
    async with on_docker(env, machine, provider) as queue:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=SHORT_HEARTBEAT)
        if kind == "cancelled":
            await asyncio.wait_for(provider.held.wait(), DOCKER_WAIT_S)
            assert len(machine.containers()) == 1, "the task had a sandbox before the cancel"
            await handle.signal(TaskWorkflow.cancel, args=["maintainer", "enough"])
        outcome = await asyncio.wait_for(handle.result(), DOCKER_WAIT_S)

    expected = {
        "pr_opened": OutcomeKind.PR_OPENED,
        "error": OutcomeKind.ERROR,
        "cancelled": OutcomeKind.CANCELLED,
        "stuck": OutcomeKind.STUCK,
    }[kind]
    assert outcome.kind is expected
    assert machine.containers() == []
    assert not machine.volume_exists()
    # What the janitor will remove in a week is still there, except after a cancel.
    assert machine.store.repo_path(machine.task_id).is_dir() is (kind != "cancelled")
    noted = [e.payload["kind"] for e in machine.recorder.events if e.type == "outcome"]
    assert noted == [expected.value]


async def test_a_task_that_fails_before_it_has_a_sandbox_leaves_nothing(
    env: WorkflowEnvironment, machine: Machine, tmp_path: Path
) -> None:
    # The source folder is gone, so prepare_workspace fails for good, and the cleanup finds
    # whatever it made by the label of the task.
    shutil.rmtree(machine.source)
    provider = Gate(write_script(tmp_path, LIST_THEN_FINISH))
    async with on_docker(env, machine, provider) as queue:
        handle = await start_task(env.client, machine.spec(), task_queue=queue)
        with pytest.raises(WorkflowFailureError):
            await asyncio.wait_for(handle.result(), DOCKER_WAIT_S)

    assert machine.containers() == []
    assert not machine.volume_exists()
