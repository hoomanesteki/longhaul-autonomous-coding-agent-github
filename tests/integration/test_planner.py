"""The planner phase inside TaskWorkflow (LH-501).

The workflow runs on a time skipping Temporal test server with the real llm_turn, tool_exec,
append_results, store_plan and post_plan activities. The model is a scripted provider. Most tests
use a fake sandbox, the memory stores and a FakeGitHub for the comments, so they need only the
Temporal test server. test_planner_cannot_mutate runs on a real container, a real bare repo and
the real prepare_workspace, because the guarantee is about a real tree.

A scripted provider that holds a role's first model call lets a test look at the workflow while
it is in one phase: PLANNING while the planner waits, CODING while the coder does. Every task id,
container, volume, task queue and bare repo has a random suffix, and each test removes what it
made.
"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker
import pytest
import yaml
from longhaul.durable.activities.plan import PlanActivities, PlanCommentActivities
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.fakes.fake_github import FAKE_BASE_URL, FakeGitHub

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import FIRST_USER_MESSAGE
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    Phase,
    Plan,
    RepoSettings,
    Role,
    Sandbox,
    SandboxLimits,
    TaskSpec,
    TaskStatus,
    TextBlock,
    ToolResultBlock,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import PLANNING_NOTE, NoCheckpoints, ToolActivities
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github.client import GitHubClient, PatAuth
from longhaul.github.side_effects import MemoryLedger, SideEffectActivities, effect_key
from longhaul.providers.base import parse_role_line
from longhaul.providers.scripted import ScriptedProvider
from longhaul.roles import planner
from longhaul.sandbox.docker_manager import TASK_LABEL, DockerSandboxManager
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
TEMPLATES = ROOT / "templates"
IMAGE = "longhaul-sandbox:py312"
PYTEST = "pytest -q"
MODULE = "src/calc/__init__.py"
REPO = "acme/calc"
BASE = "b" * 40
WAIT_S = 60.0
DOCKER_WAIT_S = 600.0
TOKEN = "ghp_planner_test_token_0123456789abcdef"
CFG = LoopConfig(model="test-model", max_turns=30, max_tokens_per_turn=1000, max_tokens_retry=5000)

PLAN_INPUT: dict[str, Any] = {
    "summary": "add() subtracts. Change the operator, then cover negative numbers.",
    "steps": [
        {
            "id": "s1",
            "description": "Change the operator in add()",
            "files": [MODULE],
            "done_when": "add(2, 3) returns 5",
        },
        {
            "id": "s2",
            "description": "Add a test for negative numbers",
            "files": ["tests/test_calc.py"],
            "done_when": "the new test passes",
        },
    ],
    "acceptance_checks": ["pytest -q", "add(-2, -3) returns -5"],
    "risks": ["Other callers may rely on the old result."],
}
PLAN = Plan.model_validate(PLAN_INPUT)
ONE_STEP = {**PLAN_INPUT, "steps": PLAN_INPUT["steps"][:1]}


# ---------- the script ----------
def tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "input": arguments}


def turn(role: str, index: int, *calls: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    respond: dict[str, Any] = {"tool_calls": list(calls)}
    if text is not None:
        respond["text"] = text
    return {"match": {"role": role, "turn_index": index}, "respond": respond}


def planner_turn(index: int, *calls: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    return turn("planner", index, *calls, text=text)


def coder_turn(index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return turn("coder", index, *calls)


def submit(arguments: dict[str, Any] = PLAN_INPUT) -> dict[str, Any]:
    return tool("submit_plan", **arguments)


FINISH = tool("finish", summary="Nothing to change.")


def planning_steps() -> list[dict[str, Any]]:
    """The planner looks around, reads a file and submits. The coder finishes at once."""
    return [
        planner_turn(0, tool("list_dir", path="."), text="Looking at the layout."),
        planner_turn(1, tool("read_file", path=MODULE)),
        planner_turn(2, submit()),
        coder_turn(0, FINISH),
    ]


def write_script(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    return path


# ---------- the provider ----------
@dataclass
class Request:
    role: str | None
    messages: list[Message]
    tool_names: list[str]


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
        self.requests.append(
            Request(role, list(kwargs["messages"]), [t.name for t in kwargs["tools"]])
        )
        gate = self.gates.get(role or "")
        if gate is not None:
            await gate.wait()
        return await super().complete(**kwargs)

    def of(self, role: str) -> list[Request]:
        return [r for r in self.requests if r.role == role]


# ---------- the worker ----------
class Boxes:
    """The workspace activities of the tests: one fake sandbox per task, holding the calc repo."""

    def __init__(self) -> None:
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        for path in sorted(CALC.rglob("*")):
            parts = set(path.relative_to(CALC).parts)
            if path.is_file() and not parts & {"REFERENCE.patch", "__pycache__", ".pytest_cache"}:
                await box.write_file(str(path.relative_to(CALC)), path.read_bytes())
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
    blobs: MemoryBlobStore
    opened: list[str] = field(default_factory=list)

    def plan_comments(self, number: int) -> list[str]:
        return [b for b in self.fake.comment_bodies(REPO, number) if "## Plan, version" in b]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


@pytest.fixture
def fake() -> FakeGitHub:
    github = FakeGitHub()
    github.seed_repo(REPO)
    return github


def github_spec(number: int, **fields: Any) -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}",
        repo=REPO,
        issue_number=number,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(repo=REPO, budgets=Budgets(**fields)),
    )


def local_spec(**fields: Any) -> TaskSpec:
    """A task on a folder: it is not picked up on GitHub, and it ends without a pull request."""
    repo = "local:/work/calc"
    return TaskSpec(
        task_id=f"lh-local-calc-{uuid.uuid4().hex[:8]}",
        repo=repo,
        issue_number=None,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(repo=repo, budgets=Budgets(**fields)),
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
            yield World(queue, provider, recorder, fake, blobs)
    finally:
        await boxes.destroy_all()
        await client.aclose()


async def wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def wait_for_phase(handle: WorkflowHandle[Any, Outcome], phase: Phase) -> TaskStatus:
    """Poll status() until the workflow reports the phase."""
    deadline = time.monotonic() + WAIT_S
    while True:
        status = await handle.query(TaskWorkflow.status)
        if status.phase is phase:
            return status
        if time.monotonic() > deadline:
            raise AssertionError(f"workflow stayed in {status.phase!r}, wanted {phase!r}")
        await asyncio.sleep(0.02)


async def result_of(handle: WorkflowHandle[Any, Outcome], wait_s: float = WAIT_S) -> Outcome:
    return await asyncio.wait_for(handle.result(), wait_s)


async def cancel(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    await handle.signal(TaskWorkflow.cancel, args=["test", "enough"])
    return await result_of(handle)


def last_text(messages: list[Message]) -> str:
    """The text blocks and tool results of the last message, joined."""
    parts: list[str] = []
    for block in messages[-1].content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ToolResultBlock):
            parts.append(block.content)
    return "\n".join(parts)


# ---------- LH-501 acceptance criteria ----------
async def test_plan_stored_and_posted(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    """The scripted planner submits. The plan is stored, posted once and shown by status()."""
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    holds = {"coder": asyncio.Event()}  # keeps the workflow in CODING, so it can be looked at
    async with running(env, write_script(tmp_path, planning_steps()), fake, gates=holds) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        coding = await wait_for_phase(handle, Phase.CODING)
        await wait_until(lambda: world.provider.of("coder"), "the first model call of the coder")

        # status() shows the version of the plan, and the phase has moved on from PLANNING.
        assert coding.plan_version == 1
        assert (await handle.query(TaskWorkflow.status)).plan_version == 1

        # Stored: one row, version 1, as the contract, waiting for a decision.
        stored = world.recorder.plans[(spec.task_id, 1)]
        assert stored.plan == PLAN
        assert stored.status == "proposed"
        assert list(world.recorder.plans) == [(spec.task_id, 1)]

        # Posted once, as a checklist, under the key of the plan.
        (comment,) = world.plan_comments(number)
        assert "- [ ] **s1**: Change the operator in add()" in comment
        assert "- [ ] **s2**: Add a test for negative numbers" in comment
        assert "- [ ] pytest -q" in comment
        assert f"<!-- longhaul:key={effect_key(spec.task_id, 'plan_comment', 1)} -->" in comment
        assert fake.count_comments(REPO, number) == 2  # the pickup comment, and the plan

        # The planner had its three turns: two tool turns and the submission. It was offered the
        # read tools and submit_plan, and the coder starts a transcript of its own.
        planned = world.provider.of("planner")
        assert len(planned) == 3
        assert set(planned[0].tool_names) == {*planner.TOOLS, "submit_plan"}
        assert [m.role for m in world.provider.of("coder")[0].messages] == ["user"]
        assert last_text(world.provider.of("coder")[0].messages) == FIRST_USER_MESSAGE

        # Every turn is a row with its role, numbered on from the planner's turns to the coder's.
        roles = [(t.turn_index, t.role) for t in world.recorder.turns]
        assert roles == [(0, "planner"), (1, "planner"), (2, "planner"), (3, "coder")]

        holds["coder"].set()
        outcome = await cancel(handle)

    assert outcome.kind is OutcomeKind.CANCELLED
    # Still one comment: nothing posted it a second time.
    assert len(world.plan_comments(number)) == 1


async def test_the_phase_is_planning_until_the_plan_is_stored(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    holds = {"planner": asyncio.Event(), "coder": asyncio.Event()}
    async with running(env, write_script(tmp_path, planning_steps()), fake, gates=holds) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        planning = await wait_for_phase(handle, Phase.PLANNING)
        await wait_until(lambda: world.provider.of("planner"), "the first call of the planner")

        assert planning.plan_version is None
        assert world.recorder.plans == {}
        assert world.plan_comments(number) == []
        assert world.provider.of("coder") == []

        holds["planner"].set()
        coding = await wait_for_phase(handle, Phase.CODING)
        assert coding.plan_version == 1

        holds["coder"].set()
        await cancel(handle)


async def test_a_local_task_stores_its_plan_and_posts_nothing(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    spec = local_spec()
    async with running(env, write_script(tmp_path, planning_steps()), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

        assert outcome.kind is OutcomeKind.PR_OPENED
        assert world.recorder.plans[(spec.task_id, 1)].plan == PLAN
        assert (await handle.query(TaskWorkflow.status)).plan_version == 1
    # The fake issue tracker has no issue and no comment: there was nowhere to post.
    assert fake.count_comments(REPO) == 0


async def test_a_task_started_without_the_plan_switch_goes_straight_to_coding(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    spec = local_spec()
    steps = [turn("coder", 0, FINISH)]
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue)
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.provider.of("planner") == []
    assert world.recorder.plans == {}
    assert status.plan_version is None


# ---------- F14: an invalid plan ----------
async def test_an_invalid_plan_gets_one_repair_turn(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    steps = [
        planner_turn(0, submit(ONE_STEP)),
        planner_turn(1, submit()),
        coder_turn(0, FINISH),
    ]
    spec = local_spec()
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

        assert outcome.kind is OutcomeKind.PR_OPENED
        asked = world.provider.of("planner")
        assert len(asked) == 2
        # The second request carries the validation error as the answer to the first call.
        repair = last_text(asked[1].messages)
        assert "steps: give 2 to 8 steps, got 1" in repair
        assert "call submit_plan again" in repair
        # Only the repaired plan is stored, once.
        assert list(world.recorder.plans) == [(spec.task_id, 1)]
        assert world.recorder.plans[(spec.task_id, 1)].plan == PLAN


async def test_a_second_invalid_plan_ends_with_an_error_outcome(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    steps = [
        planner_turn(0, submit(ONE_STEP)),
        planner_turn(1, submit(ONE_STEP)),
        coder_turn(0, FINISH),
    ]
    number = fake.seed_issue(REPO, "add() subtracts")
    spec = github_spec(number)
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.ERROR
    assert outcome.summary.startswith("planner submitted an invalid plan twice: ")
    assert "steps: give 2 to 8 steps, got 1" in outcome.summary
    assert status.phase is Phase.FAILED
    assert status.outcome == outcome
    assert status.plan_version is None
    # No plan was stored or posted, and the coder never ran.
    assert world.recorder.plans == {}
    assert world.plan_comments(number) == []
    assert world.provider.of("coder") == []
    assert len(world.provider.of("planner")) == 2


async def test_a_planner_that_never_submits_ends_after_its_turn_limit(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    steps = [
        {
            "match": {"role": "planner"},
            "respond": {"tool_calls": [tool("search", pattern="add")]},
        },
        coder_turn(0, FINISH),
    ]
    spec = local_spec()
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.ERROR
    assert outcome.summary == f"planner submitted no plan in {acts.PLANNER_MAX_TURNS} turns"
    assert len(world.provider.of("planner")) == acts.PLANNER_MAX_TURNS == 20
    assert world.provider.of("coder") == []
    assert world.recorder.plans == {}


async def test_a_planner_reply_without_a_call_gets_one_nudge_for_the_plan(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    steps = [
        {"match": {"role": "planner", "turn_index": 0}, "respond": {"text": "I have a plan."}},
        planner_turn(1, submit()),
        coder_turn(0, FINISH),
    ]
    spec = local_spec()
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

        assert outcome.kind is OutcomeKind.PR_OPENED
        second = world.provider.of("planner")[1]
        assert last_text(second.messages).endswith(planner.NUDGE)
        assert "finish" not in planner.NUDGE
        assert (spec.task_id, 1) in world.recorder.plans


async def test_two_planner_replies_in_a_row_without_a_call_end_the_run(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    steps = [{"match": {"role": "planner"}, "respond": {"text": "Thinking."}}]
    spec = local_spec()
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.ERROR
    assert outcome.summary == "no tool calls"
    assert world.recorder.plans == {}


async def test_the_planning_phase_counts_against_the_turn_budget(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    steps = [
        {"match": {"role": "planner"}, "respond": {"tool_calls": [tool("search", pattern="a")]}}
    ]
    spec = local_spec(task_turns=3)
    async with running(env, write_script(tmp_path, steps), fake) as world:
        handle = await start_task(env.client, spec, task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="turn limit")
    assert len(world.provider.of("planner")) == 3


# ---------- a plan outlives a hand-over ----------
async def test_a_plan_is_not_made_again_after_a_hand_over(
    env: WorkflowEnvironment, fake: FakeGitHub, tmp_path: Path
) -> None:
    steps = [
        planner_turn(0, submit()),
        coder_turn(0, tool("read_file", path=MODULE)),
        coder_turn(1, tool("read_file", path=MODULE)),
        coder_turn(2, tool("read_file", path=MODULE)),
        coder_turn(3, FINISH),
    ]
    spec = local_spec()
    async with running(env, write_script(tmp_path, steps), fake) as world:
        # The run hands over after every second turn, so the coder's work spans several runs.
        handle = await start_task(
            env.client,
            spec,
            task_queue=world.queue,
            plan=True,
            tuning=acts.Tuning(continue_as_new_every_turns=2),
        )
        outcome = await result_of(handle)

        assert outcome.kind is OutcomeKind.PR_OPENED
        assert len(world.provider.of("planner")) == 1
        assert list(world.recorder.plans) == [(spec.task_id, 1)]
        assert any(e.type == "continue_as_new" for e in world.recorder.events)
        status = await handle.query(TaskWorkflow.status)
        assert status.plan_version == 1


# ---------- the planner cannot change the tree ----------
@dataclass
class Looked:
    status: str = ""
    listing: str = ""
    module: str = ""
    head: str = ""


class Looking(ScriptedProvider):
    """A scripted provider that looks at the workspace just before the coder's first call."""

    def __init__(self, script: Path, look: Callable[[], Any]) -> None:
        super().__init__(script)
        self._look = look
        self.looked = False

    async def complete(self, **kwargs: Any) -> LLMResponse:
        if parse_role_line(kwargs["system"]) == "coder" and not self.looked:
            self.looked = True
            await self._look()
        return await super().complete(**kwargs)


def containers_of(task_id: str) -> list[Any]:
    return docker.from_env().containers.list(filters={"label": f"{TASK_LABEL}={task_id}"})


async def test_planner_cannot_mutate(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """A planner run_command that writes a file leaves the tree clean before coding starts.

    The command makes a new file and edits a tracked one, on a real container. When the coder
    is about to be asked, the tree is the base commit again: nothing changed, nothing new, no
    checkpoint, and the checkpoint branch has not moved.
    """
    task_id = f"lh-plan{uuid.uuid4().hex[:8]}-42"
    source = shutil.copytree(CALC, tmp_path / "calc")
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    manager = DockerSandboxManager()
    recorder = MemoryRecorder()
    repo = f"local:{source}"
    spec = TaskSpec(
        task_id=task_id,
        repo=repo,
        issue_number=42,
        title="add() subtracts",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(
            repo=repo, test_command=PYTEST, setup_command="", budgets=Budgets(task_turns=10)
        ),
    )
    steps = [
        planner_turn(
            0,
            tool(
                "run_command",
                command=(
                    "echo 'planner was here' > planner-note.txt"
                    f" && echo '# edited' >> {MODULE}"
                    " && git status --short"
                ),
            ),
        ),
        planner_turn(1, submit()),
        coder_turn(0, FINISH),
    ]
    looked = Looked()

    async def look() -> None:
        (container,) = containers_of(task_id)
        sandbox = await manager.get(container.id)
        assert sandbox is not None
        looked.status = (await sandbox.exec("git status --porcelain", timeout_s=60)).stdout
        looked.listing = (await sandbox.exec("ls -A", timeout_s=60)).stdout
        looked.module = (await sandbox.read_file(MODULE)).decode()
        looked.head = (await sandbox.exec("git rev-parse HEAD", timeout_s=60)).stdout.strip()

    provider = Looking(write_script(tmp_path, steps), look)
    registry = default_registry(test_command=PYTEST, blobs=blobs)
    llm = LlmActivities(
        provider,
        blobs,
        recorder,
        registry.specs(),
        lambda role: CFG,
        role_tools={Role.PLANNER: planner.tool_specs(registry.specs())},
    )
    tools = ToolActivities(registry, manager, blobs, recorder, checkpoints=store)
    workspace = WorkspaceActivities(manager, store, recorder, image=IMAGE, limits=SandboxLimits())
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
                *workspace.activities,
            ],
            workflow_runner=workflow_runner(),
        ):
            handle = await start_task(env.client, spec, task_queue=queue, plan=True)
            outcome = await result_of(handle, DOCKER_WAIT_S)
            status = await handle.query(TaskWorkflow.status)
        base = await store.head(task_id)

        assert outcome.kind is OutcomeKind.PR_OPENED, outcome.summary
        assert provider.looked
        # Before the coder's first call the tree is the checkpoint and nothing else.
        assert looked.status == ""
        assert "planner-note.txt" not in looked.listing
        assert "# edited" not in looked.module
        assert looked.head == base
        # No checkpoint was made, and the one the workflow holds is still the base.
        assert recorder.checkpoints == []
        assert status.last_checkpoint == base

        # The command did run and wrote both files: it saw them in its own git status.
        (row,) = [r for r in recorder.tool_calls.values() if r.name == "run_command"]
        assert row.ok and row.post_sha is None
        assert row.output_ref is not None
        text = (await blobs.get_by_key(row.output_ref)).decode()
        assert "?? planner-note.txt" in text
        assert f"M {MODULE}" in text
        assert text.endswith(PLANNING_NOTE)

        # The plan was stored all the same.
        assert recorder.plans[(task_id, 1)].plan == PLAN
    finally:
        await manager.cleanup_task(task_id)
        await store.cleanup_task(task_id)
