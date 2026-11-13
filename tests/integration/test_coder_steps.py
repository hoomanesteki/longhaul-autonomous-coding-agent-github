"""The coder works a plan step by step inside TaskWorkflow (LH-503).

A scripted planner submits a plan, and a scripted coder works it: `update_step` marks a step, a
step that is `done` leaves a `step` checkpoint at the head, and the pull request gets one commit
for each done step. The workflow runs on a time skipping Temporal test server with the real
llm_turn, tool_exec, append_results, store_plan, record_step and delivery activities.

Two kinds of worker are used. Most tests use a fake sandbox, the memory stores and a FakeGitHub
for the comments, so they need only the Temporal test server. The two tests that the story names
(test_step_checkpoints and test_pr_commits_per_step) run a task of an issue on a real container,
a real bare repo and a FakeGitHub whose remote is a local bare repo, so the checkpoints are real
commits and the pull request branch is built from them. Run `make sandbox-image` first. The setup
command is empty, so nothing is installed and the tests need no outbound network.

Every task id, container, volume, task queue and bare repo has a random suffix, and each test
removes what it made.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter
from temporalio import activity
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from tests.fakes.calc_remote import Github, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub, git

from longhaul.agent import rules
from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import FIRST_USER_MESSAGE, render_plan
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.context.compact import NOTES_TOOL_NAME
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    Phase,
    Plan,
    ProgressNotes,
    RepoSettings,
    Role,
    Sandbox,
    SandboxLimits,
    TaskSpec,
    TextBlock,
    ToolResultBlock,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.context import ContextActivities
from longhaul.durable.activities.delivery import DeliveryActivities
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.plan import (
    PlanActivities,
    PlanCommentActivities,
    StepActivities,
)
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.durable.client import start_task
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import LOST_CALL, NOT_RUN_AFTER_LOSS, TaskWorkflow
from longhaul.github.side_effects import MemoryLedger, SideEffectActivities
from longhaul.providers.base import parse_role_line
from longhaul.providers.scripted import ScriptedProvider
from longhaul.roles import coder, planner, verifier
from longhaul.sandbox.docker_manager import DockerSandboxManager
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import Context
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
TEMPLATES = ROOT / "templates"
IMAGE = "longhaul-sandbox:py312"
PYTEST = "pytest -q"
CHECK_ADD = "PYTHONPATH=src python -c 'from calc import add; assert add(2, 3) == 5'"
MODULE = "src/calc/__init__.py"
NEW_TEST_FILE = "tests/test_negative.py"
CHANGELOG = "CHANGELOG.md"
REPO = "acme/calc"
BASE = "b" * 40
AUTHOR = "longhaul <longhaul@users.noreply.github.com>"
WAIT_S = 60.0
DOCKER_WAIT_S = 600.0
CFG = LoopConfig(model="test-model", max_turns=30, max_tokens_per_turn=1000, max_tokens_retry=5000)
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=2)
CONTEXT = Context(
    window_tokens=200_000,
    compact_at=0.6,
    hard_limit=0.85,
    keep_recent_turns=6,
    elide_tool_outputs_after_turns=4,
    tool_output_max_tokens=2_000,
)
SUMMARY = "Fixed add() so it returns the sum of its arguments. All tests pass."
NEW_TEST = (
    "from calc import add\n\n\ndef test_add_negative() -> None:\n    assert add(-2, -3) == -5\n"
)

PLAN_INPUT: dict[str, Any] = {
    "summary": "add() subtracts. Change the operator, test it and note it.",
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
            "files": [NEW_TEST_FILE],
            "done_when": "the new test passes",
        },
        {
            "id": "s3",
            "description": "Note the fix in the changelog",
            "files": [CHANGELOG],
            "done_when": "the changelog names the fix",
        },
    ],
    "acceptance_checks": ["pytest -q"],
    "risks": [],
}


# ---------- the script ----------
def tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "input": arguments}


def turn(role: str, index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return {"match": {"role": role, "turn_index": index}, "respond": {"tool_calls": list(calls)}}


def coder_turn(index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return turn("coder", index, *calls)


def step(step_id: str, status: str, note: str | None = None) -> dict[str, Any]:
    arguments = {"step_id": step_id, "status": status}
    if note is not None:
        arguments["note"] = note
    return tool("update_step", **arguments)


# The compactor model writes the question for a person when the coder asks one (LH-506).
ASK_SUMMARY: dict[str, Any] = {
    "match": {"role": "compactor", "user_text_contains": "Why the agent is stuck"},
    "respond": {
        "tool_calls": [
            tool(
                "record_stuck_summary",
                goal="Fix add() in calc.",
                tried=["Read the module", "Marked the first step as doing"],
                guess="add() should take numbers of any kind.",
                question="Should add() take floats?",
            )
        ]
    },
}

FIX = tool("edit_file", path=MODULE, old_str="    return a - b", new_str="    return a + b")
ADD_TEST = tool("write_file", path=NEW_TEST_FILE, content=NEW_TEST)
ADD_NOTE = tool("write_file", path=CHANGELOG, content="- add() adds again\n")
SUBMIT = tool("submit_for_review", summary=SUMMARY)
# A submission is reviewed (LH-504). These scripts are about the coder, so the review passes.
VERDICT = tool("submit_verdict", passed=True, tests_run=PYTEST, tests_summary="3 passed", issues=[])
REVIEW = turn("verifier", 0, VERDICT)


def three_step_run() -> list[dict[str, Any]]:
    """The planner submits three steps. The coder does each one and marks it done."""
    return [
        turn("planner", 0, tool("submit_plan", **PLAN_INPUT)),
        coder_turn(0, step("s1", "doing"), FIX),
        coder_turn(1, step("s1", "done", "add() adds")),
        coder_turn(2, ADD_TEST, step("s2", "done")),
        coder_turn(3, ADD_NOTE, step("s3", "done")),
        coder_turn(4, SUBMIT),
        REVIEW,
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
    system: str


class Probe(ScriptedProvider):
    """A scripted provider that keeps its requests and can hold one call of the coder.

    `hold_coder_call` is the number of coder calls to let through before the next one waits for
    `gate`, so a test can look at the workflow while the coder is between two turns.
    """

    def __init__(self, script: Path, hold_coder_call: int | None = None) -> None:
        super().__init__(script)
        self.requests: list[Request] = []
        self.gate = asyncio.Event()
        self.hold_coder_call = hold_coder_call

    async def complete(self, **kwargs: Any) -> LLMResponse:
        role = parse_role_line(kwargs["system"])
        names = [t.name for t in kwargs["tools"]]
        self.requests.append(Request(role, list(kwargs["messages"]), names, kwargs["system"]))
        if role == "coder" and len(self.of("coder")) - 1 == self.hold_coder_call:
            await self.gate.wait()
        return await super().complete(**kwargs)

    def of(self, role: str) -> list[Request]:
        return [r for r in self.requests if r.role == role]


def blocks(message: Message) -> list[str]:
    return [b.text for b in message.content if isinstance(b, TextBlock)]


def results_of(message: Message) -> list[str]:
    return [b.content for b in message.content if isinstance(b, ToolResultBlock)]


# ---------- a worker with a fake sandbox ----------
class Boxes:
    """The workspace activities of the light tests: one fake sandbox per task, with calc in it.

    With `gone` set, the tool activities never find a sandbox, as if the container died on
    every call, and prepare_workspace and ensure_sandbox still answer.
    """

    def __init__(self, gone: bool = False) -> None:
        self.boxes: dict[str, FakeSandbox] = {}
        self.gone = gone

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return None if self.gone else self.boxes.get(sandbox_id)

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


class SpyContext(ContextActivities):
    """compact that keeps what the workflow asked it, and the tools it counted with."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.asked: list[acts.CompactInput] = []
        self.counted_with: list[list[str]] = []

    def _tools_of(self, role: Role, *, planned: bool) -> Any:
        found = super()._tools_of(role, planned=planned)
        self.counted_with.append([tool.name for tool in found])
        return found

    @activity.defn(name=acts.COMPACT.name)
    async def compact(self, inp: acts.CompactInput) -> acts.CompactOutput:
        self.asked.append(inp)
        return await super().compact(inp)


@dataclass
class Light:
    queue: str
    provider: Probe
    recorder: MemoryRecorder
    blobs: MemoryBlobStore
    context: SpyContext


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


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
async def light(
    env: WorkflowEnvironment,
    script: Path,
    *,
    hold_coder_call: int | None = None,
    lose_sandbox: bool = False,
) -> AsyncIterator[Light]:
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    provider = Probe(script, hold_coder_call)
    registry = default_registry(test_command=PYTEST, blobs=blobs)
    llm = LlmActivities(
        provider,
        blobs,
        recorder,
        registry.specs(),
        lambda role: CFG,
        role_tools={
            Role.PLANNER: planner.tool_specs(registry.specs()),
            Role.VERIFIER: verifier.tool_specs(registry.specs()),
        },
        planned_tools=coder.tool_specs(registry.specs()),
    )
    # `tools_for` is wired as build_parts wires it: the counts of a compaction are made with the
    # tools of the role whose transcript it is.
    questions = SpyContext(
        provider,
        blobs,
        registry.specs(),
        lambda role: CFG,
        CONTEXT,
        templates=TEMPLATES,
        tools_for=llm.tools_for,
    )
    boxes = Boxes(gone=lose_sandbox)
    tools = ToolActivities(registry, boxes, blobs, recorder, checkpoints=NoCheckpoints())
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
                boxes.prepare_workspace,
                boxes.cleanup,
                boxes.ensure_sandbox,
                boxes.bundle_checkpoint,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield Light(queue, provider, recorder, blobs, questions)
    finally:
        await boxes.destroy_all()


async def wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def result_of(handle: WorkflowHandle[Any, Outcome], wait_s: float = WAIT_S) -> Outcome:
    return await asyncio.wait_for(handle.result(), wait_s)


async def planned_status(handle: WorkflowHandle[Any, Outcome]) -> acts.PlannedStatus:
    # The result type can be set only for a query asked for by name.
    return await handle.query("status", result_type=acts.PlannedStatus)


def statuses(plan: Plan | None) -> list[str]:
    assert plan is not None
    return [s.status for s in plan.steps]


def plan_with(*now: str) -> Plan:
    """The plan of the scripts, with the status of each step as given."""
    plan = Plan.model_validate(PLAN_INPUT)
    steps = [
        s.model_copy(update={"status": status}) for s, status in zip(plan.steps, now, strict=True)
    ]
    return plan.model_copy(update={"steps": steps})


# ---------- the plan with its statuses ----------
async def test_plan_statuses_show_in_the_first_message_and_in_status(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The coder reads the plan in its first message, and each update changes what it reads.

    The coder's fourth call is held, so status() is looked at between two turns. By then s1 is
    done and s2 is doing.
    """
    steps = [
        turn("planner", 0, tool("submit_plan", **PLAN_INPUT)),
        coder_turn(0, step("s1", "doing")),
        coder_turn(1, FIX, step("s1", "done", "add() adds")),
        coder_turn(2, step("s2", "doing")),
        coder_turn(3, SUBMIT),
        REVIEW,
    ]
    async with light(env, write_script(tmp_path, steps), hold_coder_call=3) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        await wait_until(lambda: len(world.provider.of("coder")) == 4, "the fourth coder call")

        shown = await planned_status(handle)
        assert shown.phase is Phase.CODING
        assert shown.plan_version == 1
        assert shown.plan == plan_with("done", "doing", "todo")

        world.provider.gate.set()
        outcome = await result_of(handle)
        final = await planned_status(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert statuses(final.plan) == ["done", "doing", "todo"]

    # The first message of every coder request is the line that starts the work, then the plan
    # as it stood when the request was made: after no update, after s1 doing, after s1 done and
    # after s2 doing.
    seen = [r.messages[0] for r in world.provider.of("coder")]
    assert [blocks(m)[0] for m in seen] == [coder.FIRST_USER_MESSAGE] * 4
    assert [blocks(m)[1] for m in seen] == [
        render_plan(plan_with(*now))
        for now in (
            ("todo", "todo", "todo"),
            ("doing", "todo", "todo"),
            ("done", "todo", "todo"),
            ("done", "doing", "todo"),
        )
    ]
    assert all(len(m.content) == 2 for m in seen)
    # Only the first message moves. The request holds the three turns before it, as they were.
    last = world.provider.of("coder")[3].messages
    assert [m.role for m in last] == ["user"] + ["assistant", "user"] * 3


async def test_the_coder_is_offered_the_step_tools_and_not_finish(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [turn("planner", 0, tool("submit_plan", **PLAN_INPUT)), coder_turn(0, SUBMIT), REVIEW]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        await result_of(handle)

    (asked,) = world.provider.of("coder")
    assert set(coder.CONTROL_TOOLS) <= set(asked.tool_names)
    assert "finish" not in asked.tool_names and "submit_plan" not in asked.tool_names
    assert asked.tool_names[-3:] == list(coder.CONTROL_TOOLS)
    assert "`submit_for_review`" in asked.system and "`finish`" not in asked.system


async def test_a_task_without_a_plan_is_unchanged(env: WorkflowEnvironment, tmp_path: Path) -> None:
    steps = [coder_turn(0, tool("finish", summary="Nothing to change."))]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.PR_OPENED
    (asked,) = world.provider.of("coder")
    assert "finish" in asked.tool_names
    assert not set(coder.CONTROL_TOOLS) & set(asked.tool_names)
    assert [blocks(m) for m in asked.messages] == [[FIRST_USER_MESSAGE]]
    assert status.plan_version is None
    assert world.recorder.plans == {}
    assert not [e for e in world.recorder.events if e.type == "step_updated"]
    assert world.recorder.checkpoints == []


# ---------- what the coder's calls come to ----------
async def test_a_step_that_is_not_in_the_plan_is_answered_and_the_task_goes_on(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [
        turn("planner", 0, tool("submit_plan", **PLAN_INPUT)),
        coder_turn(0, step("s9", "done")),
        coder_turn(1, step("s1", "sleeping")),
        coder_turn(2, SUBMIT),
        REVIEW,
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        outcome = await result_of(handle)
        final = await planned_status(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    asked = world.provider.of("coder")
    assert "unknown step 's9'" in results_of(asked[1].messages[-1])[0]
    assert "status must be one of" in results_of(asked[2].messages[-1])[0]
    assert statuses(final.plan) == ["todo", "todo", "todo"]
    assert world.recorder.checkpoints == []
    assert not [e for e in world.recorder.events if e.type == "step_updated"]


async def test_every_update_is_an_event_and_only_done_leaves_a_checkpoint(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with light(env, write_script(tmp_path, three_step_run())) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        await result_of(handle)
        task_id = handle.id

    updates = [e.payload for e in world.recorder.events if e.type == "step_updated"]
    assert [(u["step_id"], u["status"]) for u in updates] == [
        ("s1", "doing"),
        ("s1", "done"),
        ("s2", "done"),
        ("s3", "done"),
    ]
    assert updates[1]["note"] == "add() adds"
    assert all(u["head_sha"] == BASE for u in updates)  # this worker takes no checkpoints
    # Each step is checkpointed once, though three of them share one sha here.
    assert [(c.task_id, c.kind, c.step_id) for c in world.recorder.checkpoints] == [
        (task_id, "step", "s1"),
        (task_id, "step", "s2"),
        (task_id, "step", "s3"),
    ]


async def test_ask_human_pauses_and_ends_stuck_when_nobody_answers(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The question is recorded and the task waits. With no answer it ends stuck (LH-506)."""
    steps = [
        turn("planner", 0, tool("submit_plan", **PLAN_INPUT)),
        coder_turn(0, step("s1", "doing"), tool("ask_human", question="Should add() take floats?")),
        ASK_SUMMARY,
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        outcome = await result_of(handle)
        status = await planned_status(handle)

    assert outcome.kind is OutcomeKind.STUCK
    assert outcome.pr_url is None
    assert "Should add() take floats?" in outcome.summary
    assert status.phase is Phase.FAILED
    assert status.outcome == outcome
    assert statuses(status.plan) == ["doing", "todo", "todo"]
    (asked,) = [e for e in world.recorder.events if e.type == "paused"]
    assert asked.payload["reason"] == "agent_asked"
    assert asked.payload["question"] == "Should add() take floats?"
    assert len(world.provider.of("coder")) == 1


async def test_submit_for_review_ends_the_task_with_its_summary(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    async with light(env, write_script(tmp_path, three_step_run())) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        outcome = await result_of(handle)
        status = await planned_status(handle)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary=SUMMARY)
    assert status.phase is Phase.DONE
    assert statuses(status.plan) == ["done", "done", "done"]


# ---------- a reply with no call, and a sandbox that is lost ----------
def last_text_of(message: Message) -> str:
    return "\n".join(blocks(message))


async def test_a_planned_coder_reply_without_a_call_is_nudged_to_submit_for_review(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A coder with a plan is not offered finish, so the nudge may not send it there."""
    steps = [
        turn("planner", 0, tool("submit_plan", **PLAN_INPUT)),
        {"match": {"role": "coder", "turn_index": 0}, "respond": {"text": "I changed add()."}},
        coder_turn(1, SUBMIT),
        REVIEW,
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary=SUMMARY)
    first, second = world.provider.of("coder")
    assert last_text_of(second.messages[-1]) == coder.NUDGE
    assert coder.NUDGE != rules.NUDGE
    # The line names only a tool that the request offered.
    named = {n for n in [*first.tool_names, "finish", "submit_plan"] if n in coder.NUDGE}
    assert named == {"submit_for_review"} and named <= set(second.tool_names)
    assert "finish" not in second.tool_names


async def test_a_task_without_a_plan_keeps_the_nudge_to_finish(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [
        {"match": {"role": "coder", "turn_index": 0}, "respond": {"text": "Done."}},
        coder_turn(1, tool("finish", summary="Nothing to change.")),
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    _, second = world.provider.of("coder")
    assert last_text_of(second.messages[-1]) == rules.NUDGE


async def test_a_lost_sandbox_keeps_the_answers_of_the_step_updates_that_ran(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The step is recorded before the sandbox is lost, and the transcript says so."""
    steps = [
        turn("planner", 0, tool("submit_plan", **PLAN_INPUT)),
        coder_turn(
            0,
            step("s9", "done"),
            step("s1", "done", "add() adds"),
            tool("run_tests"),
            FIX,
        ),
    ]
    async with light(env, write_script(tmp_path, steps), lose_sandbox=True) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        outcome = await result_of(handle)
        final = await planned_status(handle)
        (stored,) = [k for k in world.blobs.objects if k.endswith("/transcript-next.json")][-1:]
        transcript = TypeAdapter(list[Message]).validate_json(world.blobs.objects[stored])

    assert outcome == Outcome(kind=OutcomeKind.ERROR, summary="sandbox lost")
    assert statuses(final.plan) == ["done", "todo", "todo"]
    assert [(c.kind, c.step_id) for c in world.recorder.checkpoints] == [("step", "s1")]
    answers = results_of(transcript[-1])
    assert "unknown step 's9'" in answers[0]
    assert answers[1] == "s1 is now done."
    assert answers[2] == LOST_CALL
    assert answers[3] == NOT_RUN_AFTER_LOSS


# ---------- hand-over and replay ----------
async def test_the_statuses_cross_a_hand_over(env: WorkflowEnvironment, tmp_path: Path) -> None:
    async with light(env, write_script(tmp_path, three_step_run())) as world:
        # A new run starts after every second turn, so the plan is carried more than once.
        handle = await start_task(
            env.client,
            local_spec(),
            task_queue=world.queue,
            plan=True,
            tuning=acts.Tuning(continue_as_new_every_turns=2),
        )
        outcome = await result_of(handle)
        final = await planned_status(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert any(e.type == "continue_as_new" for e in world.recorder.events)
    assert statuses(final.plan) == ["done", "done", "done"]
    last = blocks(world.provider.of("coder")[-1].messages[0])
    assert "s3. [done]" in last[1]
    assert len(world.provider.of("planner")) == 1, "the plan is not made again"


async def test_histories_of_a_planned_run_replay(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """The workflow makes the same commands when the history of a planned run is replayed (F13)."""
    asked = [
        turn("planner", 0, tool("submit_plan", **PLAN_INPUT)),
        coder_turn(0, step("s9", "done"), step("s1", "doing"), FIX),
        coder_turn(1, step("s1", "done")),
        coder_turn(2, tool("ask_human", question="Which one?")),
        ASK_SUMMARY,
    ]
    async with light(env, write_script(tmp_path, three_step_run())) as world:
        finished = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        await result_of(finished)
        chained = await start_task(
            env.client,
            local_spec(),
            task_queue=world.queue,
            plan=True,
            tuning=acts.Tuning(continue_as_new_every_turns=2),
        )
        await result_of(chained)
        first_run = env.client.get_workflow_handle(
            chained.id, run_id=chained.first_execution_run_id
        )
        histories = [
            await finished.fetch_history(),
            await first_run.fetch_history(),
            await env.client.get_workflow_handle(chained.id).fetch_history(),
        ]
    async with light(env, write_script(tmp_path, asked)) as world:
        stuck = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        await result_of(stuck)
        histories.append(await stuck.fetch_history())

    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=guarded_data_converter,
    )
    for history in histories:
        await replayer.replay_workflow(history)


# ---------- a task of an issue, on a real container ----------
@dataclass
class Real:
    client: Client
    task_id: str
    number: int
    queue: str
    fake: FakeGitHub
    github: Github
    store: GitCheckpointStore
    manager: DockerSandboxManager
    recorder: MemoryRecorder
    provider: Probe
    base_sha: str
    spec: TaskSpec = field(init=False)

    def __post_init__(self) -> None:
        self.spec = TaskSpec(
            task_id=self.task_id,
            repo=REPO,
            issue_number=self.number,
            title="add() subtracts",
            body="`calc.add` subtracts its arguments instead of adding them.",
            # The review runs this command, and the image has no pytest, so it checks the fix with
            # plain python. A pull request is ready only when a command backed the verdict.
            settings=RepoSettings(repo=REPO, test_command=CHECK_ADD, setup_command=""),
        )

    def bare(self, *args: str) -> str:
        return git(f"--git-dir={self.store.repo_path(self.task_id)}", *args)


@asynccontextmanager
async def real(env: WorkflowEnvironment, tmp_path: Path, script: Path) -> AsyncIterator[Real]:
    fake = FakeGitHub()
    base_sha = seed_calc(fake, tmp_path / "gh")
    number = fake.seed_issue(REPO, "add() subtracts")
    task_id = f"lh-coder{uuid.uuid4().hex[:8]}-{number}"
    github = github_for(fake)
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    manager = DockerSandboxManager()
    recorder = MemoryRecorder()
    provider = Probe(script)
    registry = default_registry(test_command=PYTEST, blobs=blobs)
    llm = LlmActivities(
        provider,
        blobs,
        recorder,
        registry.specs(),
        lambda role: CFG,
        role_tools={
            Role.PLANNER: planner.tool_specs(registry.specs()),
            Role.VERIFIER: verifier.tool_specs(registry.specs()),
        },
        planned_tools=coder.tool_specs(registry.specs()),
    )
    tools = ToolActivities(registry, manager, blobs, recorder, checkpoints=store)
    workspace = WorkspaceActivities(
        manager, store, recorder, image=IMAGE, limits=SandboxLimits(), github=github.remote
    )
    ledger = MemoryLedger()
    delivery = DeliveryActivities(
        store,
        github.remote,
        github.client,
        ledger,
        recorder,
        blobs,
        author=AUTHOR,
        templates=TEMPLATES,
    )
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *tools.activities,
                *workspace.activities,
                *EventActivities(recorder).activities,
                *PlanActivities(recorder).activities,
                *StepActivities(recorder).activities,
                *PlanCommentActivities(github.client, ledger, templates=TEMPLATES).activities,
                *SideEffectActivities(github.client, ledger).activities,
                *delivery.activities,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield Real(
                env.client,
                task_id,
                number,
                queue,
                fake,
                github,
                store,
                manager,
                recorder,
                provider,
                base_sha,
            )
    finally:
        await manager.cleanup_task(task_id)
        await store.cleanup_task(task_id)
        await github.aclose()


@asynccontextmanager
async def issue_run(
    env: WorkflowEnvironment, tmp_path: Path
) -> AsyncIterator[tuple[Real, Outcome]]:
    """The three-step run on a task of an issue: the world it ran in, and how it ended."""
    script = write_script(tmp_path, three_step_run())
    async with real(env, tmp_path, script) as world:
        handle = await start_task(
            world.client, world.spec, task_queue=world.queue, plan=True, tuning=SHORT_HEARTBEAT
        )
        yield world, await result_of(handle, DOCKER_WAIT_S)


async def test_step_checkpoints(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """A 3-step scripted run gives 3 step checkpoints, one for each step, at the head it had."""
    async with issue_run(env, tmp_path) as (world, outcome):
        assert outcome.kind is OutcomeKind.PR_OPENED, outcome.summary

        found = await world.recorder.step_checkpoints(world.task_id)
        assert [(c.kind, c.step_id) for c in found] == [
            ("step", "s1"),
            ("step", "s2"),
            ("step", "s3"),
        ]
        # Each is the commit that the last file change before it left, so it is a real checkpoint
        # of the task, and the three are three different commits.
        saved = [c.sha for c in world.recorder.checkpoints if c.kind == "tool"]
        assert len(saved) == 3
        assert [c.sha for c in found] == saved
        assert len(set(saved)) == 3
        assert all(c.turn_index > 0 for c in found)
        for sha in saved:
            assert world.bare("cat-file", "-t", sha) == "commit"
        # The last one is where the scratch branch ended.
        assert found[-1].sha == await world.store.head(world.task_id)


async def test_pr_commits_per_step(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """The pull request branch has one commit for each step, and together they are the work."""
    async with issue_run(env, tmp_path) as (world, outcome):
        assert outcome.kind is OutcomeKind.PR_OPENED, outcome.summary
        assert outcome.pr_url is not None

        branch = f"longhaul-pr/{world.number}"
        pushed = world.fake.branches(REPO)[branch]
        commits = world.bare("rev-list", "--reverse", f"{world.base_sha}..{pushed}").splitlines()
        subjects = [world.bare("log", "-1", "--format=%s", sha) for sha in commits]
        assert subjects == [
            "fix(calc): change the operator in add()",
            "fix(calc): add a test for negative numbers",
            "fix(calc): note the fix in the changelog",
        ]
        assert world.bare("rev-parse", f"{commits[0]}^") == world.base_sha
        assert all(world.bare("log", "-1", "--format=%an <%ae>", sha) == AUTHOR for sha in commits)

        # Together the commits are the change of the scratch branch, to the byte.
        scratch = await world.store.head(world.task_id)
        pr_diff = await world.store.diff(world.task_id, pushed)
        assert pr_diff == await world.store.diff(world.task_id, scratch) != ""
        assert world.bare("rev-parse", f"{pushed}^{{tree}}") == world.bare(
            "rev-parse", f"{scratch}^{{tree}}"
        )

        # Each commit holds the work of its own step.
        touched = [
            world.bare("diff", "--name-only", f"{sha}^", sha).splitlines() for sha in commits
        ]
        assert touched == [[MODULE], [NEW_TEST_FILE], [CHANGELOG]]

        (pull,) = world.fake.pulls(REPO)
        assert pull["title"] == "fix(calc): fix add() so it returns the sum of its arguments"
        # The review passed, so the pull request is ready and says what was verified.
        assert pull["draft"] is False
        assert "Verified: 3 passed" in pull["body"]


# ---------- a compaction is counted for the role it belongs to (LH-991) ----------
def notes_turn() -> dict[str, Any]:
    notes = ProgressNotes(
        goal="Fix add() in calc",
        plan_status=[],
        files_touched=[MODULE],
        decisions=[],
        failing_tests=[],
        open_hypotheses=[],
        tried_and_failed=[],
        covers_turns_up_to=2,
    )
    call = tool(NOTES_TOOL_NAME, **notes.model_dump(mode="json"))
    return {"match": {"role": "compactor"}, "respond": {"tool_calls": [call]}}


def after_notes(index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    """A coder turn that answers only a request that carries the notes of a compaction. Put
    before the refusal, so that the retry finds it and the first request does not."""
    found = coder_turn(index, *calls)
    found["match"]["request_contains"] = "turn_offset="
    return found


def overflow_at(index: int) -> dict[str, Any]:
    """The provider refuses the request of this coder turn once, which forces a compaction."""
    return {
        "match": {"role": "coder", "turn_index": index},
        "respond": {"raise": "context_too_long"},
    }


async def test_a_planned_coder_is_compacted_with_the_planned_prompt_and_tools(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The workflow tells the activity that the transcript is a planned coder's, and the
    activity counts it with the step tools and not with the ones of an unplanned coder. Before
    this the counts of every role were made with the one list of the coder (LH-501)."""
    steps = [
        turn("planner", 0, tool("submit_plan", **PLAN_INPUT)),
        coder_turn(0, step("s1", "doing"), FIX),
        coder_turn(1, step("s1", "done", "add() adds")),
        coder_turn(2, ADD_TEST, step("s2", "done")),
        coder_turn(3, ADD_NOTE, step("s3", "done")),
        after_notes(4, SUBMIT),
        overflow_at(4),
        REVIEW,
        notes_turn(),
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    (asked,) = world.context.asked
    assert asked.role is Role.CODER
    assert asked.planned is True
    (counted,) = world.context.counted_with
    assert set(coder.CONTROL_TOOLS) <= set(counted)
    assert "finish" not in counted


async def test_an_unplanned_coder_is_compacted_with_the_unplanned_tools(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    read = tool("read_file", path=MODULE)
    steps = [
        *(coder_turn(index, read) for index in range(4)),
        after_notes(4, tool("finish", summary=SUMMARY)),
        overflow_at(4),
        notes_turn(),
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    (asked,) = world.context.asked
    assert asked.planned is False
    (counted,) = world.context.counted_with
    assert "finish" in counted
    assert not set(coder.CONTROL_TOOLS) & set(counted)
