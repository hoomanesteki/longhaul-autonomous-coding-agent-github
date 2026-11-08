"""The pause, ask and hint flow inside TaskWorkflow (LH-506).

When the stuck detector fires, or the coder calls `ask_human`, the workflow asks the compactor
model for a short question, shows it to a person, sets the phase to PAUSED and waits for a hint,
a budget top-up or a cancel. The workflow runs on a time skipping Temporal test server with the
real llm_turn, tool_exec, append_results, summarize_for_human and record_event activities, a fake
sandbox and the memory stores. A task of an issue also gets the real GitHub writes against a
FakeGitHub, so the comment, its key and the label can be read back.

The wait is 24 hours, which the test server skips while a test waits for the result. A test that
needs the task to stay paused polls the status instead, and sends its signal when it is ready.
tests/e2e/test_stuck_flow.py runs the same flow through the poller and a real worker.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from temporalio import activity
from temporalio.client import WorkflowHandle, WorkflowHistory
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from tests.fakes.calc_remote import Github, github_for, seed_calc
from tests.fakes.fake_github import FakeGitHub

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.context.human_summary import SUMMARY_TOOL_NAME
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
    TextBlock,
)
from longhaul.cost.ledger import MemoryLedger as CostLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.context import ContextActivities
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.plan import PlanActivities, StepActivities
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.client import start_task
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.github.side_effects import MemoryLedger, SideEffectActivities
from longhaul.providers.base import parse_role_line
from longhaul.providers.scripted import ScriptedProvider
from longhaul.roles import coder, planner
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import Context
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
PRICES = Pricing.load(ROOT / "config" / "pricing.yaml")
BASE = "b" * 40
REPO = "acme/calc"
WAIT_S = 60.0
CFG = LoopConfig(model="claude-haiku-5-5", max_turns=60, max_tokens_per_turn=1000)
CONTEXT = Context(
    window_tokens=200_000,
    compact_at=0.6,
    hard_limit=0.85,
    keep_recent_turns=6,
    elide_tool_outputs_after_turns=4,
    tool_output_max_tokens=2_000,
)
SUMMARY = "Gave up on the lookup."
KEY_ERROR = "KeyError: 'sku-7'"
FAILING_COMMAND = f'echo "{KEY_ERROR}" >&2; exit 1'
QUESTION = "Which copy of inv should the tests import?"
HINT = "the tests import the copy in vendor/, edit that one"
NEEDS_HUMAN = "longhaul:needs-human"

PLAN: dict[str, Any] = {
    "summary": "Fix the lookup.",
    "steps": [
        {"id": "s1", "description": "Fix it", "files": ["inv.py"], "done_when": "tests pass"},
        {"id": "s2", "description": "Test it", "files": ["t.py"], "done_when": "tests pass"},
    ],
    "acceptance_checks": ["pytest -q"],
    "risks": [],
}


# ---------- the script ----------
def tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "input": arguments}


def turn(role: str, index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return {"match": {"role": role, "turn_index": index}, "respond": {"tool_calls": list(calls)}}


FAIL = tool("run_command", command=FAILING_COMMAND)
FINISH = tool("finish", summary=SUMMARY)

# The compactor answers the question for a person. A request for notes has other words in it.
SUMMARY_STEP = {
    "match": {"role": "compactor", "user_text_contains": "Why the agent is stuck"},
    "respond": {
        "tool_calls": [
            tool(
                SUMMARY_TOOL_NAME,
                goal="Fix the sku lookup in inv.py.",
                tried=["Ran the failing command three times", "Read inv.py", "Read the test"],
                guess="The tests import a second copy of inv.",
                question=QUESTION,
            )
        ]
    },
}


def loop_then_finish(failures: int) -> list[dict[str, Any]]:
    """A coder that fails the same way `failures` times, then finishes."""
    steps = [turn("coder", i, FAIL) for i in range(failures)]
    return [*steps, turn("coder", failures, FINISH), SUMMARY_STEP]


def write_script(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    return path


# ---------- the provider ----------
class Probe(ScriptedProvider):
    """A scripted provider that keeps its requests and can hold the call for the question.

    With `hold_summary` the compactor call waits for `gate`, and `held` is set while it waits,
    so a test can send a signal while the summary is being written.
    """

    def __init__(self, script: Path, *, hold_summary: bool = False) -> None:
        super().__init__(script)
        self.requests: list[tuple[str | None, list[Message]]] = []
        self.hold_summary = hold_summary
        self.held = asyncio.Event()
        self.gate = asyncio.Event()

    async def complete(self, **kwargs: Any) -> LLMResponse:
        role = parse_role_line(kwargs["system"])
        self.requests.append((role, list(kwargs["messages"])))
        if role == "compactor" and self.hold_summary:
            self.held.set()
            await self.gate.wait()
        return await super().complete(**kwargs)

    def of(self, role: str) -> list[list[Message]]:
        return [messages for r, messages in self.requests if r == role]


def texts(messages: list[Message]) -> list[str]:
    return [b.text for m in messages for b in m.content if isinstance(b, TextBlock)]


# ---------- a worker with a fake sandbox ----------
class Boxes:
    def __init__(self) -> None:
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        await box.write_file("notes.txt", b"one\ntwo\n")
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
    provider: Probe
    recorder: MemoryRecorder
    costs: CostLedger
    fake: FakeGitHub | None = None
    number: int | None = None

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.recorder.events if e.type == kind]

    def spec(self) -> TaskSpec:
        """The task of this world: of the issue when there is a FakeGitHub, else of a folder."""
        if self.number is not None:
            return TaskSpec(
                task_id=f"lh-pause{uuid.uuid4().hex[:8]}-{self.number}",
                repo=REPO,
                issue_number=self.number,
                title="lookup fails",
                body="`inv.lookup` raises KeyError for a sku that exists.",
                settings=RepoSettings(repo=REPO, budgets=Budgets()),
            )
        repo = "local:/work/inv"
        return TaskSpec(
            task_id=f"lh-local-inv-{uuid.uuid4().hex[:8]}",
            repo=repo,
            issue_number=None,
            title="lookup fails",
            body="`inv.lookup` raises KeyError for a sku that exists.",
            settings=RepoSettings(repo=repo, budgets=Budgets()),
        )


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


@asynccontextmanager
async def light(
    env: WorkflowEnvironment,
    script: Path,
    *,
    hold_summary: bool = False,
    tmp_path: Path | None = None,
) -> AsyncIterator[World]:
    """A worker. With `tmp_path` the issue is on a FakeGitHub and the task is one of an issue."""
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    costs = CostLedger(PRICES)
    provider = Probe(script, hold_summary=hold_summary)
    registry = default_registry(test_command="true", blobs=blobs)

    def config_for(role: Role) -> LoopConfig:
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
    boxes = Boxes()
    tools = ToolActivities(registry, boxes, blobs, recorder, checkpoints=NoCheckpoints())
    github: Github | None = None
    fake: FakeGitHub | None = None
    number: int | None = None
    writes: list[Any] = []
    if tmp_path is not None:
        fake = FakeGitHub()
        seed_calc(fake, tmp_path / "gh")
        number = fake.seed_issue(REPO, "lookup fails")
        github = github_for(fake)
        writes = SideEffectActivities(github.client, MemoryLedger()).activities
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
                *writes,
                boxes.prepare_workspace,
                boxes.cleanup,
                boxes.ensure_sandbox,
                boxes.bundle_checkpoint,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield World(queue, provider, recorder, costs, fake, number)
    finally:
        await boxes.destroy_all()
        if github is not None:
            await github.aclose()


async def wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.02)


async def result_of(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    return await asyncio.wait_for(handle.result(), WAIT_S)


async def paused(world: World, handle: WorkflowHandle[Any, Outcome], count: int = 1) -> None:
    """Wait until the task has asked its `count`th question and is waiting for an answer."""
    await wait_until(lambda: len(world.events("paused")) >= count, f"pause {count}")
    status = await handle.query(TaskWorkflow.status)
    assert status.phase is Phase.PAUSED


async def send_hint(handle: WorkflowHandle[Any, Outcome], text: str = HINT) -> None:
    await handle.signal(TaskWorkflow.hint, args=["maintainer", text])


# ---------- a hint ----------
async def test_a_hint_resumes_the_task_and_it_finishes(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await paused(world, handle)
        status = await handle.query(TaskWorkflow.status)
        assert status.paused_reason is not None and "repeated failure" in status.paused_reason
        await send_hint(handle)
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.PR_OPENED and outcome.summary == SUMMARY
    assert final.phase is Phase.DONE and final.paused_reason is None
    assert [e.type for e in world.recorder.events if e.type in ("stuck", "paused", "resumed")] == [
        "stuck",
        "paused",
        "resumed",
    ]
    (asked,) = world.events("paused")
    assert asked["reason"] == "repeated_failure" and asked["pause"] == 1
    assert asked["question"] == QUESTION
    # A local task has no issue to comment on, so the comment is recorded in the event.
    for heading in ("What I'm trying to do", "What I tried", "My current guess", "Question"):
        assert f"### {heading}" in asked["comment"]
    (resumed,) = world.events("resumed")
    assert resumed["pause"] == 1 and resumed["via"] == "hint"


async def test_the_model_sees_the_hint_on_the_turn_after_the_pause(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await paused(world, handle)
        await send_hint(handle)
        await result_of(handle)

    coder_requests = world.provider.of("coder")
    assert len(coder_requests) == 4
    assert f"Hint from maintainer: {HINT}" in texts(coder_requests[3])
    assert all(f"Hint from maintainer: {HINT}" not in texts(r) for r in coder_requests[:3])


async def test_the_pause_starts_from_a_clean_window(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """Two failures after the hint are not a third: the pattern builds up again from nothing."""
    script = write_script(tmp_path, loop_then_finish(5))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await paused(world, handle)
        await send_hint(handle)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert len(world.events("paused")) == 1


async def test_the_summary_is_a_billed_call_with_a_ledger_row(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await paused(world, handle)
        await send_hint(handle)
        await result_of(handle)

    models = [(e.turn_index, e.model) for e in world.costs.entries]
    assert (2, "claude-haiku-5-5") in models
    assert len(world.provider.of("compactor")) == 1


async def test_a_hint_sent_while_the_summary_is_written_is_not_lost(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script, hold_summary=True) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await asyncio.wait_for(world.provider.held.wait(), WAIT_S)
        await send_hint(handle)
        world.provider.gate.set()
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert len(world.events("resumed")) == 1


async def test_a_hint_that_arrives_before_the_pause_does_not_answer_it(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A hint given while the task was still working was an answer to nothing yet."""
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await send_hint(handle, "an early hint")
        await paused(world, handle)
        assert world.events("resumed") == []
        await send_hint(handle)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED


# ---------- add_budget, cancel and the timeout ----------
async def test_a_budget_top_up_also_resumes_the_task(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await paused(world, handle)
        await handle.signal(TaskWorkflow.add_budget, args=["maintainer", 1.0, 5])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    (resumed,) = world.events("resumed")
    assert resumed["via"] == "budget"


async def test_a_cancel_ends_a_paused_task(env: WorkflowEnvironment, tmp_path: Path) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await paused(world, handle)
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "not worth it"])
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.CANCELLED
    assert status.phase is Phase.CANCELLED
    assert world.events("resumed") == []


async def test_a_pause_that_nobody_answers_ends_stuck_after_the_wait(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        outcome = await result_of(handle)  # the test server skips the 24 hours
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.STUCK
    assert "24 hours" in outcome.summary and "failed 3 times" in outcome.summary
    assert status.phase is Phase.FAILED and status.outcome == outcome
    assert len(world.events("paused")) == 1 and world.events("resumed") == []


async def test_the_wait_for_a_hint_can_be_set_for_a_run(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(
            env.client,
            world.spec(),
            task_queue=world.queue,
            tuning=acts.Tuning(hint_wait_s=90),
        )
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.STUCK
    assert "90 seconds" in outcome.summary


async def test_a_task_gets_three_pauses_and_the_fourth_ends_it(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(15))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        for pause in (1, 2, 3):
            await paused(world, handle, pause)
            await send_hint(handle, f"hint {pause}")
            await wait_until(lambda n=pause: len(world.events("resumed")) >= n, f"resume {pause}")
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.STUCK
    assert "3 hints" in outcome.summary
    assert [p["pause"] for p in world.events("paused")] == [1, 2, 3]
    assert len(world.events("stuck")) == 4, "the fourth signal is recorded and not asked about"
    assert len(world.provider.of("compactor")) == 3


# ---------- a question of the coder ----------
async def test_ask_human_pauses_the_task_and_a_hint_goes_back_to_coding(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [
        turn("planner", 0, tool("submit_plan", **PLAN)),
        turn("coder", 0, tool("ask_human", question="Should lookup accept a list of skus?")),
        {
            "match": {"role": "coder", "turn_index": 1},
            "respond": {"tool_calls": [tool("submit_for_review", summary=SUMMARY)]},
        },
        SUMMARY_STEP,
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue, plan=True)
        await paused(world, handle)
        await send_hint(handle, "yes, a list of skus")
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED and outcome.summary == SUMMARY
    (asked,) = world.events("paused")
    assert asked["reason"] == "agent_asked"
    request = world.provider.of("compactor")[0]
    assert "Should lookup accept a list of skus?" in texts(request)[0]
    after = world.provider.of("coder")[1]
    assert "Hint from maintainer: yes, a list of skus" in texts(after)


async def test_a_question_then_a_hint_clears_the_failures_that_came_before_it(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The window still holds two failures when the coder asks, and the hint empties it.

    One more failure of the same kind after the hint would be the third in a row if the window
    were kept, and the task would pause again at once. A window that is empty needs three.
    """
    steps = [
        turn("planner", 0, tool("submit_plan", **PLAN)),
        turn("coder", 0, FAIL),
        turn("coder", 1, FAIL),
        turn("coder", 2, tool("ask_human", question="Which copy of inv do the tests import?")),
        turn("coder", 3, FAIL),
        turn("coder", 4, tool("submit_for_review", summary=SUMMARY)),
        SUMMARY_STEP,
    ]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue, plan=True)
        await paused(world, handle)
        await send_hint(handle)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.events("stuck") == [], "no rule fired: the question was the only pause"
    (asked,) = world.events("paused")
    assert asked["reason"] == "agent_asked"
    assert len(world.provider.of("coder")) == 5


# ---------- the size of a hint ----------
async def test_a_huge_hint_is_cut_before_it_reaches_the_model(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await paused(world, handle)
        await send_hint(handle, "word " * 60_000)  # 300 KB
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    shown = [t for t in texts(world.provider.of("coder")[3]) if t.startswith("Hint from")]
    (hint,) = shown
    assert len(hint.encode()) < 3_000
    assert "hint cut here" in hint


# ---------- a task of an issue ----------
async def test_a_task_of_an_issue_asks_once_with_a_key_and_moves_the_label(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, loop_then_finish(15))
    async with light(env, script, tmp_path=tmp_path) as world:
        assert world.fake is not None and world.number is not None
        spec = world.spec()
        handle = await start_task(env.client, spec, task_queue=world.queue)
        await paused(world, handle, 1)

        key = f"<!-- longhaul:key={spec.task_id}:stuck:1 -->"
        assert world.fake.count_comments(REPO, world.number, containing=key) == 1
        assert NEEDS_HUMAN in world.fake.label_names(REPO, world.number)
        comment = next(c for c in world.fake.comment_bodies(REPO, world.number) if key in c)
        assert "### Question" in comment and QUESTION in comment

        await send_hint(handle)
        await wait_until(lambda: len(world.events("resumed")) >= 1, "the resume")
        assert NEEDS_HUMAN not in world.fake.label_names(REPO, world.number)

        # The next pause is a second effect: its own comment and its own label change.
        await paused(world, handle, 2)
        key2 = f"<!-- longhaul:key={spec.task_id}:stuck:2 -->"
        assert world.fake.count_comments(REPO, world.number, containing=key2) == 1
        assert world.fake.count_comments(REPO, world.number, containing=key) == 1
        assert NEEDS_HUMAN in world.fake.label_names(REPO, world.number)
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "enough"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.CANCELLED
    assert world.fake.count_comments(REPO, world.number, containing=":stuck:") == 2


# ---------- replay ----------
async def test_the_history_of_a_pause_replays(env: WorkflowEnvironment, tmp_path: Path) -> None:
    script = write_script(tmp_path, loop_then_finish(3))
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(), task_queue=world.queue)
        await paused(world, handle)
        await send_hint(handle)
        await result_of(handle)
        resumed = await handle.fetch_history()
    async with light(env, script) as world:
        timed_out = await start_task(env.client, world.spec(), task_queue=world.queue)
        await result_of(timed_out)
        ended = await timed_out.fetch_history()

    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=guarded_data_converter,
    )
    for history in (resumed, ended):
        await replayer.replay_workflow(history)


@pytest.mark.parametrize("name", ["stuck_signal_recorded", "ask_human_ended"])
async def test_histories_from_before_the_pause_replay(name: str) -> None:
    """A run that began before this story keeps the behaviour it recorded (F13).

    The two files were recorded with the code of the commit before LH-506. In the first, a stuck
    signal was only noted and the task went on. In the second, `ask_human` noted the question and
    ended the task as stuck. Neither has the marker of the pause, so the new code takes the old
    way at the same places and makes the same commands.
    """
    path = ROOT / "tests" / "fixtures" / "histories" / f"{name}.json"
    history = WorkflowHistory.from_json(f"replay-{name}", path.read_text(encoding="utf-8"))
    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=guarded_data_converter,
    )
    await replayer.replay_workflow(history)
