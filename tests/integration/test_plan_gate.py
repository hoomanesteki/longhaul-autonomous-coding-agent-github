"""The human plan gate inside TaskWorkflow (LH-502).

A repo whose settings have `plan_approval_required` holds the plan after the planner submits it.
The task waits in the phase AWAITING_PLAN_APPROVAL with the label `longhaul:needs-human` on its
issue, until a maintainer approves (`/longhaul approve`, the signal approve_plan), rejects with
feedback (`/longhaul reject <feedback>`, reject_plan), or the wait runs out. A rejection has the
planner revise its plan as the next version, at most three in all.

The workflow runs on a time skipping Temporal test server with the real llm_turn, tool_exec,
append_results, store_plan, decide_plan, post_plan and GitHub write activities. The model is a
scripted provider, the sandbox is a fake and GitHub is a FakeGitHub, so the tests need only the
Temporal test server. The server skips the hours of a wait, so a test that waits a day takes a
moment.

A scripted provider that holds a role's first model call lets a test look at the workflow while
it is in one phase, or send a signal before the workflow has reached the wait. Every task id and
task queue has a random suffix.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    Phase,
    RepoSettings,
    Role,
    Sandbox,
    TaskSpec,
    TaskStatus,
    TextBlock,
    ToolResultBlock,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.plan import PlanActivities, PlanCommentActivities
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.client import start_task
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.side_effects import MemoryLedger, SideEffectActivities, effect_key
from longhaul.providers.base import parse_role_line
from longhaul.providers.scripted import ScriptedProvider
from longhaul.roles import planner
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
PYTEST = "pytest -q"
REPO = "acme/calc"
BASE = "b" * 40
WAIT_S = 60.0
TOKEN = "ghp_plan_gate_test_token_0123456789abcdef"
CFG = LoopConfig(model="test-model", max_turns=30, max_tokens_per_turn=1000, max_tokens_retry=5000)
# A cancel reaches a running activity at its next heartbeat. The timeouts of section 3.7 would
# make each test that cancels a held model call wait ten seconds or more.
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=2)
NEEDS_HUMAN = "longhaul:needs-human"
DAY = timedelta(hours=24)


def plan_input(summary: str) -> dict[str, Any]:
    return {
        "summary": summary,
        "steps": [
            {
                "id": "s1",
                "description": "Change the operator in add()",
                "files": ["src/calc/__init__.py"],
                "done_when": "add(2, 3) returns 5",
            },
            {
                "id": "s2",
                "description": "Add a test for negative numbers",
                "files": ["tests/test_calc.py"],
                "done_when": "the new test passes",
            },
        ],
        "acceptance_checks": [PYTEST, "add(-2, -3) returns -5"],
        "risks": [],
    }


V1 = plan_input("Version one: change the operator, then cover negative numbers.")
V2 = plan_input("Version two: the same change, checked with pytest -x.")
V3 = plan_input("Version three: the same change, with the tests first.")


# ---------- the script ----------
def tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "input": arguments}


def turn(role: str, index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return {"match": {"role": role, "turn_index": index}, "respond": {"tool_calls": list(calls)}}


def planner_turn(index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return turn("planner", index, *calls)


def coder_turn(index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return turn("coder", index, *calls)


def submit(arguments: dict[str, Any]) -> dict[str, Any]:
    return tool("submit_plan", **arguments)


FINISH = tool("finish", summary="Nothing to change.")


def write_script(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    return path


# ---------- the provider ----------
@dataclass
class Request:
    role: str | None
    messages: list[Message]


class Gated(ScriptedProvider):
    """A scripted provider that keeps its requests and can hold the first call of a role.

    A gate is an event. While it is not set, every call of that role waits. The activity
    heartbeats while it waits, as it does for a slow model.
    """

    def __init__(self, script: Path, gates: dict[str, asyncio.Event] | None = None) -> None:
        super().__init__(script)
        self.gates = gates or {}
        self.requests: list[Request] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        role = parse_role_line(kwargs["system"])
        self.requests.append(Request(role, list(kwargs["messages"])))
        gate = self.gates.get(role or "")
        if gate is not None:
            await gate.wait()
        return await super().complete(**kwargs)

    def of(self, role: str) -> list[Request]:
        return [r for r in self.requests if r.role == role]


# ---------- the worker ----------
class Boxes:
    """The workspace activities of the tests: one empty fake sandbox per task."""

    def __init__(self) -> None:
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        self.boxes[box.id] = box
        return acts.PrepareWorkspaceOutput(sandbox_id=box.id, base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        return None

    @activity.defn(name=acts.ENSURE_SANDBOX.name)
    async def ensure_sandbox(self, inp: acts.EnsureSandboxInput) -> str:
        return inp.sandbox_id or f"sbx-{inp.task_id}"

    @activity.defn(name=acts.BUNDLE_CHECKPOINT.name)
    async def bundle_checkpoint(self, inp: acts.BundleCheckpointInput) -> BlobRef:
        key = f"tasks/{inp.task_id}/ckpt/{inp.turn_index}.bundle"
        return BlobRef(key=key, sha256="0" * 64, size=1)

    async def destroy_all(self) -> None:
        for box in self.boxes.values():
            await box.destroy()


@dataclass
class World:
    queue: str
    provider: Gated
    recorder: MemoryRecorder
    fake: FakeGitHub

    def plan_comments(self, number: int) -> list[str]:
        return [b for b in self.fake.comment_bodies(REPO, number) if "## Plan, version" in b]

    def review_comments(self, number: int) -> list[str]:
        return [b for b in self.fake.comment_bodies(REPO, number) if "/longhaul approve" in b]

    def review_labels(self, number: int) -> list[str]:
        """The needs-human label changes on the issue, in order: "labeled" and "unlabeled"."""
        return [e.action for e in self.fake.label_events(REPO, number) if e.label == NEEDS_HUMAN]

    def decisions(self, spec: TaskSpec) -> list[tuple[int, str, str | None]]:
        """(version, status, decided_by) of every stored version of the plan, in order."""
        return [
            (version, record.status, record.decided_by)
            for (task_id, version), record in sorted(self.recorder.plans.items())
            if task_id == spec.task_id
        ]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=guarded_data_converter
    ) as environment:
        yield environment


@pytest.fixture
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO)
    return github


def github_spec(number: int, *, gate: bool = True) -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}",
        repo=REPO,
        issue_number=number,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(repo=REPO, plan_approval_required=gate, budgets=Budgets()),
    )


def local_spec(*, gate: bool = True) -> TaskSpec:
    """A task on a folder: it is not picked up on GitHub, and it ends without a pull request."""
    repo = "local:/work/calc"
    return TaskSpec(
        task_id=f"lh-local-calc-{uuid.uuid4().hex[:8]}",
        repo=repo,
        issue_number=None,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(repo=repo, plan_approval_required=gate),
    )


@asynccontextmanager
async def running(
    env: WorkflowEnvironment,
    script: Path,
    fake: FakeGitHub,
    *,
    gates: dict[str, asyncio.Event] | None = None,
) -> AsyncIterator[World]:
    """A worker with the real activities for one test. Yields what the test looks at."""
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    provider = Gated(script, gates)
    registry = default_registry(test_command=PYTEST, blobs=blobs)
    llm = LlmActivities(
        provider,
        blobs,
        recorder,
        registry.specs(),
        lambda role: CFG,
        role_tools={Role.PLANNER: planner.tool_specs(registry.specs())},
    )
    boxes = Boxes()
    tools = ToolActivities(registry, boxes, blobs, recorder, checkpoints=NoCheckpoints())
    client = GitHubClient(FAKE_BASE_URL, PatAuth(TOKEN), transport=fake.transport())
    ledger = MemoryLedger()
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *tools.activities,
                *EventActivities(recorder).activities,
                *PlanActivities(recorder).activities,
                *PlanCommentActivities(client, ledger, templates=TEMPLATES).activities,
                *SideEffectActivities(client, ledger).activities,
                boxes.prepare_workspace,
                boxes.cleanup,
                boxes.ensure_sandbox,
                boxes.bundle_checkpoint,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield World(queue, provider, recorder, fake)
    finally:
        await boxes.destroy_all()
        await client.aclose()


async def wait_until(condition: Callable[[], object], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def wait_for_status(
    handle: WorkflowHandle[Any, Outcome], ready: Callable[[TaskStatus], bool], what: str
) -> TaskStatus:
    """Poll status() until `ready` holds for it."""
    deadline = time.monotonic() + WAIT_S
    while True:
        status = await handle.query(TaskWorkflow.status)
        if ready(status):
            return status
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}; the workflow is {status!r}")
        await asyncio.sleep(0.02)


async def wait_for_phase(handle: WorkflowHandle[Any, Outcome], phase: Phase) -> TaskStatus:
    return await wait_for_status(handle, lambda s: s.phase is phase, f"phase {phase.value}")


async def wait_for_version(handle: WorkflowHandle[Any, Outcome], version: int) -> TaskStatus:
    """Wait until the plan of that version is out and the workflow waits for a decision on it."""
    return await wait_for_status(
        handle,
        lambda s: s.phase is Phase.AWAITING_PLAN_APPROVAL and s.plan_version == version,
        f"a decision on version {version}",
    )


async def result_of(handle: WorkflowHandle[Any, Outcome], wait_s: float = WAIT_S) -> Outcome:
    return await asyncio.wait_for(handle.result(), wait_s)


async def cancel(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    await handle.signal(TaskWorkflow.cancel, args=["test", "enough"])
    return await result_of(handle)


async def reject(handle: WorkflowHandle[Any, Outcome], by: str, feedback: str) -> None:
    await handle.signal(TaskWorkflow.reject_plan, args=[by, feedback])


def last_text(messages: list[Message]) -> str:
    """The text blocks and tool results of the last message, joined."""
    parts: list[str] = []
    for block in messages[-1].content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            parts.append(block.content)
    return "\n".join(parts)


async def replays(handle: WorkflowHandle[Any, Outcome]) -> None:
    """Replay the history of a finished run on the current workflow code. A decision that the
    code takes differently the second time is a non-determinism error."""
    history = await handle.fetch_history()
    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=guarded_data_converter,
    )
    await replayer.replay_workflow(history)


# ---------- the acceptance criteria of LH-502 ----------
async def test_approve_proceeds(env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path) -> None:
    """The task waits for a person. An approval two hours later starts the coding."""
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    holds = {"coder": asyncio.Event()}  # keeps the workflow in CODING, so it can be looked at
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        waiting = await wait_for_version(handle, 1)
        await wait_until(lambda: world.review_comments(number), "the request for a decision")

        # The plan is stored and posted, the issue asks for a person, and nothing codes yet.
        assert waiting.outcome is None
        assert world.decisions(spec) == [(1, "proposed", None)]
        assert len(world.plan_comments(number)) == 1
        assert world.review_labels(number) == ["labeled"]
        assert NEEDS_HUMAN in fake.label_names(REPO, number)
        assert world.provider.of("coder") == []

        started = await env.get_current_time()
        await env.sleep(timedelta(hours=2))
        assert await env.get_current_time() - started >= timedelta(hours=2)
        assert (await handle.query(TaskWorkflow.status)).phase is Phase.AWAITING_PLAN_APPROVAL
        assert world.provider.of("coder") == []

        await handle.signal(TaskWorkflow.approve_plan, "alice")
        await wait_until(lambda: world.provider.of("coder"), "the first model call of the coder")
        coding = await handle.query(TaskWorkflow.status)

        # Coding has started with the plan that was approved, and the person is not asked again.
        assert coding.phase is Phase.CODING
        assert coding.plan_version == 1
        assert world.decisions(spec) == [(1, "approved", "alice")]
        assert world.review_labels(number) == ["labeled", "unlabeled"]
        assert NEEDS_HUMAN not in fake.label_names(REPO, number)
        assert len(world.provider.of("planner")) == 1

        outcome = await cancel(handle)

    assert outcome.kind is OutcomeKind.CANCELLED


async def test_reject_revises(env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path) -> None:
    """A rejection has the planner revise the plan as version 2. Approving that one starts the
    coding. Each version is asked about, posted and decided on by itself."""
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    steps = [planner_turn(0, submit(V1)), planner_turn(1, submit(V2)), coder_turn(0, FINISH)]
    holds = {"coder": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        await wait_for_version(handle, 1)
        await reject(handle, "alice", "Run the tests with -x so the first failure shows.")
        revised = await wait_for_version(handle, 2)
        await wait_until(lambda: len(world.review_comments(number)) == 2, "the second request")

        # Version 1 is rejected, version 2 waits, and the coder has not started.
        assert revised.plan_version == 2
        assert world.decisions(spec) == [(1, "rejected", "alice"), (2, "proposed", None)]
        assert world.review_labels(number) == ["labeled", "unlabeled", "labeled"]
        assert world.provider.of("coder") == []

        # The planner went on in its own transcript, and the feedback is a user message in it.
        asked = world.provider.of("planner")
        assert len(asked) == 2
        assert [m.role for m in asked[1].messages] == ["user", "assistant", "user"]
        feedback = last_text(asked[1].messages)
        assert "Run the tests with -x so the first failure shows." in feedback
        assert "version 1" in feedback
        assert "submit_plan" in feedback

        await handle.signal(TaskWorkflow.approve_plan, "bob")
        await wait_until(lambda: world.provider.of("coder"), "the first model call of the coder")

        assert world.decisions(spec) == [(1, "rejected", "alice"), (2, "approved", "bob")]
        assert world.review_labels(number) == ["labeled", "unlabeled", "labeled", "unlabeled"]
        assert (await handle.query(TaskWorkflow.status)).plan_version == 2

        # Both versions are on the issue, under keys of their own.
        first, second = world.plan_comments(number)
        assert "Version one" in first and "Version two" in second
        assert effect_key(spec.task_id, "plan_comment", 2) in second

        # The coder starts a transcript of its own and never reads the review.
        (opening,) = world.provider.of("coder")[0].messages
        assert opening.role == "user"
        assert not any("rejected" in last_text(r.messages) for r in world.provider.of("coder"))

        outcome = await cancel(handle)

    assert outcome.kind is OutcomeKind.CANCELLED


async def test_timeout_stuck(env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path) -> None:
    """Nobody answers for a day. The task ends as stuck, and the person is still asked."""
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        await wait_for_version(handle, 1)

        # The task still waits a minute before the day is up and has ended a minute after it. The
        # clock of the test server jumps far ahead once the workflow is over, so the end is
        # looked at between two sleeps and not measured afterwards.
        await env.sleep(DAY - timedelta(minutes=1))
        assert (await handle.query(TaskWorkflow.status)).phase is Phase.AWAITING_PLAN_APPROVAL
        await env.sleep(timedelta(minutes=2))
        await wait_for_status(handle, lambda s: s.outcome is not None, "the end of the wait")

        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.STUCK
    assert outcome.summary == "plan not approved: no decision in 24 hours"
    assert outcome.pr_url is None
    assert (status.phase, status.outcome) == (Phase.FAILED, outcome)
    # Nobody decided, so the plan is as it was proposed, and the coder never ran.
    assert world.decisions(spec) == [(1, "proposed", None)]
    assert world.provider.of("coder") == []
    # The issue still asks for a person: the task ended because none came.
    assert world.review_labels(number) == ["labeled"]
    ended = [e for e in world.recorder.events if e.type == acts.OUTCOME_EVENT]
    assert [e.payload["kind"] for e in ended] == ["stuck"]


async def test_gate_off_skips(env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path) -> None:
    """A repo without the setting goes from the plan to the coder, as it did before the gate."""
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number, gate=False)
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    holds = {"coder": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        await wait_until(lambda: world.provider.of("coder"), "the first model call of the coder")
        coding = await handle.query(TaskWorkflow.status)

        assert coding.phase is Phase.CODING
        assert coding.plan_version == 1
        # No request for a person, no label, and the plan was never put to a decision.
        assert world.review_labels(number) == []
        assert world.review_comments(number) == []
        assert world.decisions(spec) == [(1, "proposed", None)]
        assert len(world.plan_comments(number)) == 1

        outcome = await cancel(handle)
        history = await handle.fetch_history()

    assert outcome.kind is OutcomeKind.CANCELLED
    # A history of a run without the gate has no marker for it, so it replays on old code too.
    assert not [e for e in history.events if "lh-502-plan-gate" in str(e)]


# ---------- signals that come early, late or twice ----------
async def test_an_approval_sent_during_planning_is_not_lost(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    """The planner is still working when a maintainer approves. The approval is kept, and the
    task goes through the gate without waiting for a day."""
    spec = local_spec()
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    holds = {"planner": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        await wait_for_phase(handle, Phase.PLANNING)
        await wait_until(lambda: world.provider.of("planner"), "the first call of the planner")
        await handle.signal(TaskWorkflow.approve_plan, "alice")
        holds["planner"].set()

        outcome = await result_of(handle)
        await replays(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.decisions(spec) == [(1, "approved", "alice")]


async def test_a_rejection_sent_during_planning_is_not_lost(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    spec = local_spec()
    steps = [planner_turn(0, submit(V1)), planner_turn(1, submit(V2)), coder_turn(0, FINISH)]
    holds = {"planner": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        await wait_until(lambda: world.provider.of("planner"), "the first call of the planner")
        await reject(handle, "alice", "Name the test file in the second step.")
        holds["planner"].set()

        await wait_for_version(handle, 2)
        # The rejection was used once. It is not the answer to version 2 as well.
        assert (await handle.query(TaskWorkflow.status)).phase is Phase.AWAITING_PLAN_APPROVAL
        await handle.signal(TaskWorkflow.approve_plan, "bob")
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.decisions(spec) == [(1, "rejected", "alice"), (2, "approved", "bob")]
    assert "Name the test file in the second step." in last_text(
        world.provider.of("planner")[1].messages
    )


async def test_an_approval_sent_during_planning_does_not_ask_for_one(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    """The answer is there before the plan is. The issue gets the plan and nothing that asks for
    a decision, because a request that stays on the issue after the answer would mislead."""
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    holds = {"planner": asyncio.Event(), "coder": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        await wait_until(lambda: world.provider.of("planner"), "the first call of the planner")
        await handle.signal(TaskWorkflow.approve_plan, "alice")
        holds["planner"].set()
        await wait_until(lambda: world.provider.of("coder"), "the first model call of the coder")

        assert len(world.plan_comments(number)) == 1
        assert world.decisions(spec) == [(1, "approved", "alice")]
        # Nobody was asked, so there is no label to take back and no request to answer.
        assert world.review_comments(number) == []
        assert world.review_labels(number) == []
        assert NEEDS_HUMAN not in fake.label_names(REPO, number)

        outcome = await cancel(handle)
        await replays(handle)

    assert outcome.kind is OutcomeKind.CANCELLED


async def test_a_rejection_sent_during_planning_asks_about_the_revision_only(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    steps = [planner_turn(0, submit(V1)), planner_turn(1, submit(V2)), coder_turn(0, FINISH)]
    holds = {"planner": asyncio.Event(), "coder": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        await wait_until(lambda: world.provider.of("planner"), "the first call of the planner")
        await reject(handle, "alice", "Name the test file in the second step.")
        holds["planner"].set()
        await wait_for_version(handle, 2)
        await wait_until(lambda: world.review_comments(number), "the request for version 2")

        # Version 1 was answered before it was out, so only version 2 asks for a person.
        (request,) = world.review_comments(number)
        assert "version 2" in request
        assert world.review_labels(number) == ["labeled"]

        await handle.signal(TaskWorkflow.approve_plan, "bob")
        await wait_until(lambda: world.provider.of("coder"), "the first model call of the coder")
        assert world.decisions(spec) == [(1, "rejected", "alice"), (2, "approved", "bob")]
        assert world.review_labels(number) == ["labeled", "unlabeled"]
        outcome = await cancel(handle)
        await replays(handle)

    assert outcome.kind is OutcomeKind.CANCELLED


async def test_an_approval_sent_while_the_planner_revises_counts_for_the_new_version(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    spec = local_spec()
    steps = [planner_turn(0, submit(V1)), planner_turn(1, submit(V2)), coder_turn(0, FINISH)]
    holds = {"planner": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        holds["planner"].set()
        await wait_for_version(handle, 1)
        holds["planner"].clear()  # the revision will wait inside its model call
        await reject(handle, "alice", "Add a risk about other callers.")
        await wait_until(lambda: len(world.provider.of("planner")) == 2, "the revision to start")
        await handle.signal(TaskWorkflow.approve_plan, "bob")
        holds["planner"].set()

        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.decisions(spec) == [(1, "rejected", "alice"), (2, "approved", "bob")]


async def test_a_decision_after_the_approval_changes_nothing(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    """A late rejection, such as a command sent twice, does not pull a task back to planning."""
    spec = local_spec()
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    holds = {"coder": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        await wait_for_version(handle, 1)
        await handle.signal(TaskWorkflow.approve_plan, "alice")
        await wait_until(lambda: world.provider.of("coder"), "the first model call of the coder")
        await reject(handle, "carol", "Too late.")
        holds["coder"].set()

        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.decisions(spec) == [(1, "approved", "alice")]
    assert len(world.provider.of("planner")) == 1


# ---------- how the gate ends ----------
async def test_the_third_rejection_ends_the_task_as_stuck(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    steps = [
        planner_turn(0, submit(V1)),
        planner_turn(1, submit(V2)),
        planner_turn(2, submit(V3)),
        coder_turn(0, FINISH),
    ]
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        for version in (1, 2, 3):
            await wait_for_version(handle, version)
            await reject(handle, "alice", f"Not yet, round {version}.")
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome == Outcome(kind=OutcomeKind.STUCK, summary="plan not approved")
    assert (status.phase, status.plan_version) == (Phase.FAILED, 3)
    assert world.decisions(spec) == [
        (1, "rejected", "alice"),
        (2, "rejected", "alice"),
        (3, "rejected", "alice"),
    ]
    # No fourth plan was asked for, and the coder never ran.
    assert len(world.provider.of("planner")) == 3
    assert world.provider.of("coder") == []
    assert len(world.plan_comments(number)) == 3
    # Each rejection is an answer, so its label comes off. The task then ends as stuck, and a
    # stuck task asks for a person (LH-507), so the label goes on once more at the end.
    assert world.review_labels(number) == ["labeled", "unlabeled"] * 3 + ["labeled"]


async def test_a_cancel_while_waiting_ends_the_task_as_cancelled(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    spec = local_spec()
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        await wait_for_version(handle, 1)
        outcome = await cancel(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.CANCELLED
    assert status.phase is Phase.CANCELLED
    assert world.decisions(spec) == [(1, "proposed", None)]


async def test_a_local_task_waits_for_a_decision_too(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    """The gate is the repo's setting, not GitHub's. A task on a folder has no issue to label or
    comment on, and a person can still answer with the signal."""
    spec = local_spec()
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        await wait_for_version(handle, 1)
        await handle.signal(TaskWorkflow.approve_plan, "alice")
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.decisions(spec) == [(1, "approved", "alice")]
    assert fake.count_comments(REPO) == 0


async def test_the_setting_without_a_planner_has_nothing_to_hold(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    """A task started without the plan switch has no plan, so the gate has nothing to hold."""
    spec = local_spec()
    steps = [coder_turn(0, FINISH)]
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.recorder.plans == {}


# ---------- replay ----------
async def test_a_history_that_waited_and_revised_replays(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    """Replaying makes the same decisions: the gate, the revision and the end are in the history
    as commands, and the code issues them in the same order."""
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    steps = [planner_turn(0, submit(V1)), planner_turn(1, submit(V2)), coder_turn(0, FINISH)]
    holds = {"coder": asyncio.Event()}
    async with running(env, write_script(tmp_path, steps), fake, gates=holds) as world:
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        await wait_for_version(handle, 1)
        await reject(handle, "alice", "Check it with -x.")
        await wait_for_version(handle, 2)
        await handle.signal(TaskWorkflow.approve_plan, "bob")
        await wait_until(lambda: world.provider.of("coder"), "the first model call of the coder")
        await cancel(handle)
        history = await handle.fetch_history()
        await replays(handle)

    # The gate records a marker, so that a run from before it replays without the gate.
    assert [e for e in history.events if "lh-502-plan-gate" in str(e)]


async def test_a_history_that_timed_out_replays(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    spec = local_spec()
    steps = [planner_turn(0, submit(V1)), coder_turn(0, FINISH)]
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)
        await replays(handle)

    assert outcome.kind is OutcomeKind.STUCK
