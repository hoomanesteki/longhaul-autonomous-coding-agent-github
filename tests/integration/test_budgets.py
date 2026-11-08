"""Budget enforcement and top-ups inside TaskWorkflow (LH-702).

Before every model turn a task that enforces budgets calls `check_budget`. When a limit would be
crossed, the task pauses, says so, and waits: for `add_budget` or `cancel` when the limit is the
task's own, and for 00:00 UTC when it is the repo's day. The workflow runs on a time skipping
Temporal test server with the real llm_turn, tool_exec, append_results and check_budget
activities, a fake sandbox, the memory ledger on the checked-in prices and a scripted provider.
A task of an issue also gets the real GitHub writes against a FakeGitHub, so the comment, its key
and the label can be read back.

The waits are 24 hours, which the test server skips while a test waits for the result. A test
that needs the task to stay paused polls the status instead and sends its signal when it is ready.
The history is the proof that nothing runs while a task waits: `timeline` lists the model turns,
the budget checks and the signals in the order the server recorded them.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    Outcome,
    OutcomeKind,
    Phase,
    RepoSettings,
    Role,
    Sandbox,
    StopReason,
    TaskSpec,
    TextBlock,
    Usage,
)
from longhaul.cost.budget import BudgetVerdict
from longhaul.cost.ledger import LedgerEntry
from longhaul.cost.ledger import MemoryLedger as CostLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.context import ContextActivities
from longhaul.durable.activities.cost import BudgetActivities, BudgetCommentActivities
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
from longhaul.roles import coder, planner, verifier
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import Context
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
PRICES = Pricing.load(ROOT / "config" / "pricing.yaml")
BASE = "b" * 40
REPO = "acme/calc"
LOCAL = "local:/work/inv"
WAIT_S = 60.0
SONNET = "claude-sonnet-5-5"
CFG = LoopConfig(model=SONNET, max_turns=60, max_tokens_per_turn=1000)
CONTEXT = Context(
    window_tokens=200_000,
    compact_at=0.6,
    hard_limit=0.85,
    keep_recent_turns=6,
    elide_tool_outputs_after_turns=4,
    tool_output_max_tokens=2_000,
)
SUMMARY = "Read the notes."
NEEDS_HUMAN = "longhaul:needs-human"
# A call that reads a file of the fake sandbox. It changes nothing and never fails, so the stuck
# detector has nothing to say about a script of these.
READ = {"name": "read_file", "input": {"path": "notes.txt"}}
FINISH = {"name": "finish", "input": {"summary": SUMMARY}}
# The work a planned coder submits is reviewed (LH-504), and this verdict passes it.
VERDICT_STEP = {
    "match": {"role": "verifier", "turn_index": 0},
    "respond": {
        "tool_calls": [
            {
                "name": "submit_verdict",
                "input": {
                    "passed": True,
                    "tests_run": "pytest -q",
                    "tests_summary": "1 passed",
                    "issues": [],
                },
            }
        ]
    },
}


# ---------- the script ----------
def reads_then_finish(reads: int) -> list[dict[str, Any]]:
    """A coder that reads `reads` times and then finishes, so it takes reads + 1 turns."""
    steps = [
        {"match": {"role": "coder", "turn_index": i}, "respond": {"tool_calls": [READ]}}
        for i in range(reads)
    ]
    steps.append(
        {"match": {"role": "coder", "turn_index": reads}, "respond": {"tool_calls": [FINISH]}}
    )
    return steps


def write_script(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    return path


# ---------- the provider ----------
class Probe(ScriptedProvider):
    """A scripted provider that counts the model calls of the coder.

    `first_replies` are answers for the first coder calls, before the script takes over. A
    script is keyed on the request, and a retry or a continuation sends the same turn again, so
    a test that needs a reply cut off at max_tokens or paused queues it here. `caps` is the
    output cap of each coder call.
    """

    def __init__(self, script: Path, first_replies: list[LLMResponse] | None = None) -> None:
        super().__init__(script)
        self.calls = 0
        self.caps: list[int] = []
        self.first_replies = list(first_replies or [])

    async def complete(self, **kwargs: Any) -> LLMResponse:
        if parse_role_line(kwargs["system"]) == "coder":
            self.calls += 1
            self.caps.append(kwargs["max_tokens"])
            if self.first_replies:
                return self.first_replies.pop(0)
        return await super().complete(**kwargs)


def cut(stop: StopReason) -> LLMResponse:
    """A coder reply that stops for `stop` with a little text, which the loop will not use."""
    return LLMResponse(
        content=[TextBlock(text="Looking at the notes first.")],
        tool_calls=[],
        stop_reason=stop,
        usage=Usage(input_tokens=2_000, output_tokens=300),
        model=SONNET,
        latency_ms=1,
    )


class Recording(BudgetActivities):
    """The check_budget activity, keeping what the workflow asked it."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.seen: list[acts.CheckBudgetInput] = []

    @activity.defn(name=acts.CHECK_BUDGET.name)
    async def check_budget(self, inp: acts.CheckBudgetInput) -> BudgetVerdict:
        self.seen.append(inp)
        return await super().check_budget(inp)


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
    checks: list[acts.CheckBudgetInput] = field(default_factory=list)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.recorder.events if e.type == kind]

    def spec(self, budgets: Budgets | None = None) -> TaskSpec:
        """The task of this world: of the issue when there is a FakeGitHub, else of a folder."""
        limits = budgets or Budgets()
        if self.number is not None:
            return TaskSpec(
                task_id=f"lh-budget{uuid.uuid4().hex[:8]}-{self.number}",
                repo=REPO,
                issue_number=self.number,
                title="read the notes",
                body="Read notes.txt.",
                settings=RepoSettings(repo=REPO, budgets=limits),
            )
        return TaskSpec(
            task_id=f"lh-local-inv-{uuid.uuid4().hex[:8]}",
            repo=LOCAL,
            issue_number=None,
            title="read the notes",
            body="Read notes.txt.",
            settings=RepoSettings(repo=LOCAL, budgets=limits),
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
    tmp_path: Path | None = None,
    first_replies: list[LLMResponse] | None = None,
) -> AsyncIterator[World]:
    """A worker. With `tmp_path` the issue is on a FakeGitHub and the task is one of an issue."""
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    costs = CostLedger(PRICES)
    provider = Probe(script, first_replies)
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
        role_tools={
            Role.PLANNER: planner.tool_specs(registry.specs()),
            Role.VERIFIER: verifier.tool_specs(registry.specs()),
        },
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
    budgets = Recording(PRICES, costs, config_for)
    boxes = Boxes()
    tools = ToolActivities(registry, boxes, blobs, recorder, checkpoints=NoCheckpoints())
    github: Github | None = None
    fake: FakeGitHub | None = None
    number: int | None = None
    writes: list[Any] = []
    if tmp_path is not None:
        fake = FakeGitHub()
        seed_calc(fake, tmp_path / "gh")
        number = fake.seed_issue(REPO, "read the notes")
        github = github_for(fake)
        shared = MemoryLedger()
        writes = [
            *SideEffectActivities(github.client, shared).activities,
            *BudgetCommentActivities(github.client, shared, templates=TEMPLATES).activities,
        ]
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *questions.activities,
                *budgets.activities,
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
            yield World(queue, provider, recorder, costs, fake, number, budgets.seen)
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


async def begin(
    env: WorkflowEnvironment, world: World, spec: TaskSpec
) -> WorkflowHandle[Any, Outcome]:
    return await start_task(env.client, spec, task_queue=world.queue, enforce_budgets=True)


async def paused(world: World, handle: WorkflowHandle[Any, Outcome], count: int = 1) -> None:
    """Wait until the task has announced its `count`th pause and waits."""
    await wait_until(lambda: len(world.events("paused")) >= count, f"pause {count}")
    status = await handle.query(TaskWorkflow.status)
    assert status.phase is Phase.PAUSED


async def top_up(handle: WorkflowHandle[Any, Outcome], usd: float = 0.0, turns: int = 0) -> None:
    await handle.signal(TaskWorkflow.add_budget, args=["maintainer", usd, turns])


def timeline(history: WorkflowHistory, *, timers: bool = False) -> list[str]:
    """The model turns, the budget checks and the signals, in recorded order.

    A wait with a timeout starts a timer that the server ends or the signal makes moot, so timers
    are left out unless a test asks for them.
    """
    names: list[str] = []
    for event in history.events:
        if event.HasField("activity_task_scheduled_event_attributes"):
            name = event.activity_task_scheduled_event_attributes.activity_type.name
            if name in ("llm_turn", "check_budget"):
                names.append(name)
        elif event.HasField("workflow_execution_signaled_event_attributes"):
            names.append(f"signal:{event.workflow_execution_signaled_event_attributes.signal_name}")
        elif timers and event.HasField("timer_started_event_attributes"):
            names.append("timer_started")
        elif timers and event.HasField("timer_fired_event_attributes"):
            names.append("timer_fired")
    return names


def assert_checked_before_every_turn(names: list[str]) -> None:
    """Every model turn comes right after a budget check, with nothing between but other work."""
    for i, name in enumerate(names):
        if name == "llm_turn":
            assert names[i - 1] == "check_budget", names


# The first coder turn of a scripted task asks for about 0.01 dollars in the worst case: the
# prompt is a few thousand tokens at 2 dollars per million, and the reply may use 1,000 tokens
# at 10 dollars per million. A limit of 0.04 therefore lets a few turns through and not the
# whole script.
SMALL_TASK = Budgets(task_usd=0.04, task_turns=60, task_wall_minutes=240, daily_repo_usd=25.0)


# ---------- the task dollar limit ----------
async def test_task_usd_pause_and_topup(env: WorkflowEnvironment, tmp_path: Path) -> None:
    script = write_script(tmp_path, reads_then_finish(8))
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(SMALL_TASK))
        await paused(world, handle)
        status = await handle.query(TaskWorkflow.status)
        assert status.paused_reason == "waiting for a budget top-up: task usd"
        turns_at_pause = world.provider.calls
        assert 0 < turns_at_pause < 9, "some turns ran and the limit stopped the rest"

        await asyncio.sleep(0.3)
        assert world.provider.calls == turns_at_pause, "nothing is asked of the model while paused"

        await top_up(handle, usd=1.0)
        outcome = await result_of(handle)
        final = await handle.query(TaskWorkflow.status)
        history = await handle.fetch_history()

    assert outcome.kind is OutcomeKind.PR_OPENED and outcome.summary == SUMMARY
    assert final.phase is Phase.DONE and final.paused_reason is None
    (asked,) = world.events("paused")
    assert asked["reason"] == "task_usd" and asked["pause"] == 1
    assert asked["numbers"]["task_usd_limit"] == 0.04
    assert asked["numbers"]["spent_task_usd"] > 0
    assert asked["turn_index"] == turns_at_pause
    (resumed,) = world.events("resumed")
    assert resumed["via"] == "budget" and resumed["pause"] == 1
    (added,) = world.events("budget_added")
    assert added["usd"] == 1.0 and added["turns"] == 0
    assert added["by"] == "maintainer", "the run report names who topped the budget up"

    names = timeline(history)
    assert_checked_before_every_turn(names)
    signal = names.index("signal:add_budget")
    assert names[signal - 1] == "check_budget", "the task stopped at a check, before a model turn"
    assert names[signal + 1] == "check_budget", "and the top-up is followed by a new check"


async def test_the_new_limit_is_the_one_in_force_after_a_top_up(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A top-up that is too small pauses the task again, with the limit it now has."""
    script = write_script(tmp_path, reads_then_finish(12))
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(SMALL_TASK))
        await paused(world, handle, 1)
        await top_up(handle, usd=0.001)  # not enough for one more turn
        await paused(world, handle, 2)
        await top_up(handle, usd=5.0)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    first, second = world.events("paused")
    assert (first["pause"], second["pause"]) == (1, 2)
    assert first["numbers"]["task_usd_limit"] == 0.04
    assert second["numbers"]["task_usd_limit"] == pytest.approx(0.041)


async def test_a_top_up_sent_before_the_pause_raises_the_limit_without_a_pause(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(5))
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(SMALL_TASK))
        await top_up(handle, usd=5.0)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.events("paused") == []


async def test_a_task_nobody_tops_up_ends_with_budget_exhausted_after_the_wait(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(8))
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(SMALL_TASK))
        outcome = await result_of(handle)  # the test server skips the 24 hours
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.BUDGET_EXHAUSTED
    assert outcome.summary == "task budget of $0.04 is spent: no top-up in 24 hours"
    assert status.phase is Phase.FAILED and status.outcome == outcome
    assert len(world.events("paused")) == 1 and world.events("resumed") == []


async def test_the_wait_for_a_top_up_can_be_set_for_a_run(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(8))
    async with light(env, script) as world:
        handle = await start_task(
            env.client,
            world.spec(SMALL_TASK),
            task_queue=world.queue,
            enforce_budgets=True,
            tuning=acts.Tuning(hint_wait_s=90),
        )
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.BUDGET_EXHAUSTED
    assert outcome.summary.endswith("no top-up in 90 seconds")


async def test_a_cancel_ends_a_task_that_waits_for_a_top_up(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(8))
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(SMALL_TASK))
        await paused(world, handle)
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "not worth it"])
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    assert outcome.kind is OutcomeKind.CANCELLED
    assert status.phase is Phase.CANCELLED
    assert world.events("resumed") == []


async def test_a_hint_does_not_answer_a_budget_pause(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(8))
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(SMALL_TASK))
        await paused(world, handle)
        await handle.signal(TaskWorkflow.hint, args=["maintainer", "keep going"])
        await asyncio.sleep(0.3)
        status = await handle.query(TaskWorkflow.status)
        assert status.phase is Phase.PAUSED, "more money is what the task waits for"
        await top_up(handle, usd=5.0)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED


# ---------- the turn limit ----------
async def test_turn_limit_pause(env: WorkflowEnvironment, tmp_path: Path) -> None:
    script = write_script(tmp_path, reads_then_finish(5))
    limits = Budgets(task_usd=50.0, task_turns=3, task_wall_minutes=240, daily_repo_usd=500.0)
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(limits))
        await paused(world, handle)
        status = await handle.query(TaskWorkflow.status)
        assert status.turn_index == 3 and world.provider.calls == 3

        await top_up(handle, turns=10)
        outcome = await result_of(handle)
        history = await handle.fetch_history()

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.provider.calls == 6, "three turns before the pause and three after it"
    (asked,) = world.events("paused")
    assert asked["reason"] == "task_turns"
    assert asked["numbers"]["turns_done"] == 3 and asked["numbers"]["turns_limit"] == 3
    (added,) = world.events("budget_added")
    assert added["turns"] == 10
    names = timeline(history)
    assert_checked_before_every_turn(names)
    assert names[names.index("signal:add_budget") - 1] == "check_budget"


async def test_a_turn_limit_nobody_lifts_ends_with_budget_exhausted(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(5))
    limits = Budgets(task_usd=50.0, task_turns=2, task_wall_minutes=240, daily_repo_usd=500.0)
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(limits))
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.BUDGET_EXHAUSTED
    assert outcome.summary == "turn budget of 2 turns is spent: no top-up in 24 hours"


# ---------- the planner ----------
PLAN: dict[str, Any] = {
    "summary": "Read the notes.",
    "steps": [
        {"id": "s1", "description": "Read", "files": ["notes.txt"], "done_when": "it is read"},
        {"id": "s2", "description": "Report", "files": [], "done_when": "it is reported"},
    ],
    "acceptance_checks": ["true"],
    "risks": [],
}


async def test_the_planner_is_held_to_the_budget_too(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [
        {"match": {"role": "planner", "turn_index": 0}, "respond": {"tool_calls": [READ]}},
        {
            "match": {"role": "planner", "turn_index": 1},
            "respond": {"tool_calls": [{"name": "submit_plan", "input": PLAN}]},
        },
        {
            "match": {"role": "coder", "turn_index": 0},
            "respond": {
                "tool_calls": [{"name": "submit_for_review", "input": {"summary": SUMMARY}}]
            },
        },
        VERDICT_STEP,
    ]
    limits = Budgets(task_usd=50.0, task_turns=1, task_wall_minutes=240, daily_repo_usd=500.0)
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(
            env.client,
            world.spec(limits),
            task_queue=world.queue,
            plan=True,
            enforce_budgets=True,
        )
        await paused(world, handle)
        status = await handle.query(TaskWorkflow.status)
        assert status.turn_index == 1, "the planner read once, and its second turn was held back"
        (asked,) = world.events("paused")
        assert asked["reason"] == "task_turns"

        await top_up(handle, turns=5)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    (resumed,) = world.events("resumed")
    assert resumed["via"] == "budget"


async def test_the_verifier_is_held_to_the_budget_too(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [
        {"match": {"role": "planner", "turn_index": 0}, "respond": {"tool_calls": [READ]}},
        {
            "match": {"role": "planner", "turn_index": 1},
            "respond": {"tool_calls": [{"name": "submit_plan", "input": PLAN}]},
        },
        {
            "match": {"role": "coder", "turn_index": 0},
            "respond": {
                "tool_calls": [{"name": "submit_for_review", "input": {"summary": SUMMARY}}]
            },
        },
        VERDICT_STEP,
    ]
    limits = Budgets(task_usd=50.0, task_turns=3, task_wall_minutes=240, daily_repo_usd=500.0)
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(
            env.client,
            world.spec(limits),
            task_queue=world.queue,
            plan=True,
            enforce_budgets=True,
        )
        await paused(world, handle)
        status = await handle.query(TaskWorkflow.status)
        assert status.turn_index == 3, "two planner turns and a coder turn, then the review waits"
        assert world.checks[-1].role is Role.VERIFIER, "the verifier's check stopped it"
        (asked,) = world.events("paused")
        assert asked["reason"] == "task_turns" and asked["turn_index"] == 3

        await top_up(handle, turns=5)
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.checks[-1].role is Role.VERIFIER
    (resumed,) = world.events("resumed")
    assert resumed["via"] == "budget"


# ---------- every model call, not only the first of a turn ----------
async def test_a_turn_that_pauses_is_checked_again_before_it_goes_on(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    # The continued request ends with the text so far, as an assistant message, which the script
    # counts as a turn of its own.
    again = {"match": {"role": "coder", "turn_index": 1}, "respond": {"tool_calls": [FINISH]}}
    script = write_script(tmp_path, [*reads_then_finish(0), again])
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=240, daily_repo_usd=500.0)
    async with light(env, script, first_replies=[cut("pause_turn")]) as world:
        handle = await begin(env, world, world.spec(limits))
        outcome = await result_of(handle)
        history = await handle.fetch_history()

    assert outcome.kind is OutcomeKind.PR_OPENED
    names = timeline(history)
    assert names == ["check_budget", "llm_turn", "check_budget", "llm_turn"]
    assert_checked_before_every_turn(names)
    assert [seen.high_output_cap for seen in world.checks] == [False, False]


async def test_a_retry_after_max_tokens_is_priced_at_its_own_cap_and_can_be_held_back(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    # The first call fits, as it is priced at 1,000 output tokens. The retry may use 64,000,
    # which is about 0.64 dollars on sonnet, and the task has 0.04 to spend.
    script = write_script(tmp_path, reads_then_finish(0))
    async with light(env, script, first_replies=[cut("max_tokens")]) as world:
        handle = await begin(env, world, world.spec(SMALL_TASK))
        await paused(world, handle)
        assert world.provider.calls == 1, "the retry was not sent"
        assert [seen.high_output_cap for seen in world.checks] == [False, True]
        (asked,) = world.events("paused")
        assert asked["reason"] == "task_usd"

        await top_up(handle, usd=5.0)
        outcome = await result_of(handle)
        history = await handle.fetch_history()

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.provider.caps == [1000, 64000]
    assert_checked_before_every_turn(timeline(history))


async def test_the_first_coder_turn_is_not_priced_with_the_planners_last_request(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [
        {"match": {"role": "planner", "turn_index": 0}, "respond": {"tool_calls": [READ]}},
        {
            "match": {"role": "planner", "turn_index": 1},
            "respond": {"tool_calls": [{"name": "submit_plan", "input": PLAN}]},
        },
        {
            "match": {"role": "coder", "turn_index": 0},
            "respond": {
                "tool_calls": [{"name": "submit_for_review", "input": {"summary": SUMMARY}}]
            },
        },
        VERDICT_STEP,
    ]
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=240, daily_repo_usd=500.0)
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(
            env.client,
            world.spec(limits),
            task_queue=world.queue,
            plan=True,
            enforce_budgets=True,
        )
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    planner_checks = [seen for seen in world.checks if seen.role is Role.PLANNER]
    (coder_check,) = [seen for seen in world.checks if seen.role is Role.CODER]
    assert planner_checks[-1].last_input_tokens > 0, "the planner's second turn follows a request"
    assert coder_check.last_input_tokens == 0, "the transcript of the coder starts empty"
    (verifier_check,) = [seen for seen in world.checks if seen.role is Role.VERIFIER]
    assert verifier_check.last_input_tokens == 0, "and so does the transcript of the verifier"


# ---------- the wall clock ----------
async def start_in_the_past(
    env: WorkflowEnvironment, world: World, spec: TaskSpec, minutes_ago: int
) -> WorkflowHandle[Any, Outcome]:
    """Start a task whose clock began `minutes_ago` minutes ago, as one that has run a while."""
    state = acts.RunState(
        spec=spec,
        phase=Phase.QUEUED,
        started_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        enforce_budgets=True,
    )
    handle: WorkflowHandle[Any, Outcome] = await env.client.start_workflow(
        TaskWorkflow.run, state, id=spec.task_id, task_queue=world.queue
    )
    return handle


async def test_wall_clock_pause(env: WorkflowEnvironment, tmp_path: Path) -> None:
    script = write_script(tmp_path, reads_then_finish(3))
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=60, daily_repo_usd=500.0)
    async with light(env, script) as world:
        handle = await start_in_the_past(env, world, world.spec(limits), minutes_ago=61)
        await paused(world, handle)
        assert world.provider.calls == 0, "the clock stopped the task before its first turn"
        (asked,) = world.events("paused")
        assert asked["reason"] == "wall_clock"
        assert asked["numbers"]["wall_clock_minutes_limit"] == 60
        assert asked["numbers"]["elapsed_minutes"] > 60

        await top_up(handle, usd=1.0)
        outcome = await result_of(handle)

    # Any top-up starts a new window, or the task would stop again at once.
    assert outcome.kind is OutcomeKind.PR_OPENED
    assert len(world.events("paused")) == 1


async def test_wall_clock_pause_in_the_middle_of_a_run(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """Time passes while the first model call is retried, and the second turn is not started."""
    # Six overloads in a row: the retries wait 2, 4, 8, 16, 32 and 64 seconds, over a minute. The
    # test server skips those waits while a test waits for the result, so nobody tops up and the
    # task runs to the end of its wait.
    overloaded = {
        "match": {"role": "coder", "turn_index": 0},
        "respond": {"raise": "overloaded", "times": 6},
    }
    script = write_script(tmp_path, [overloaded, *reads_then_finish(3)])
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=1, daily_repo_usd=500.0)
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(limits))
        outcome = await result_of(handle)

    (asked,) = world.events("paused")
    assert asked["reason"] == "wall_clock" and asked["turn_index"] == 1
    assert asked["numbers"]["elapsed_minutes"] > 1
    # The overloads were not billed, so the ledger holds the one turn that was answered.
    assert {e.turn_index for e in world.costs.entries} == {0}, "the second turn was not started"
    assert outcome.kind is OutcomeKind.BUDGET_EXHAUSTED
    assert outcome.summary == "time budget of 1 minute is spent: no top-up in 24 hours"


async def test_time_spent_waiting_for_a_hint_is_not_time_spent_working(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A person who answers after three hours does not use up the task's hour."""
    failing = {"name": "run_command", "input": {"command": 'echo "KeyError: x" >&2; exit 1'}}
    steps: list[dict[str, Any]] = [
        {"match": {"role": "coder", "turn_index": i}, "respond": {"tool_calls": [failing]}}
        for i in range(3)
    ]
    steps.append({"match": {"role": "coder", "turn_index": 3}, "respond": {"tool_calls": [FINISH]}})
    steps.append(
        {
            "match": {"role": "compactor", "user_text_contains": "Why the agent is stuck"},
            "respond": {
                "tool_calls": [
                    {
                        "name": SUMMARY_TOOL_NAME,
                        "input": {
                            "goal": "Read the notes.",
                            "tried": ["Ran it three times", "Read it", "Read it again"],
                            "guess": "The command is wrong.",
                            "question": "What should it run?",
                        },
                    }
                ]
            },
        }
    )
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=60, daily_repo_usd=500.0)
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await begin(env, world, world.spec(limits))
        await paused(world, handle)
        await env.sleep(timedelta(hours=3))
        await handle.signal(TaskWorkflow.hint, args=["maintainer", "run true"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert [e["reason"] for e in world.events("paused")] == ["repeated_failure"]


async def test_time_spent_waiting_for_a_plan_decision_is_not_time_spent_working(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A person who approves the plan after three hours does not use up the task's hour."""
    steps = [
        {"match": {"role": "planner", "turn_index": 0}, "respond": {"tool_calls": [READ]}},
        {
            "match": {"role": "planner", "turn_index": 1},
            "respond": {"tool_calls": [{"name": "submit_plan", "input": PLAN}]},
        },
        {
            "match": {"role": "coder", "turn_index": 0},
            "respond": {
                "tool_calls": [{"name": "submit_for_review", "input": {"summary": SUMMARY}}]
            },
        },
        VERDICT_STEP,
    ]
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=60, daily_repo_usd=500.0)
    async with light(env, write_script(tmp_path, steps)) as world:
        spec = world.spec(limits)
        gated = spec.settings.model_copy(update={"plan_approval_required": True})
        spec = spec.model_copy(update={"settings": gated})
        handle = await start_task(
            env.client, spec, task_queue=world.queue, plan=True, enforce_budgets=True
        )

        async def held() -> bool:
            status = await handle.query(TaskWorkflow.status)
            return status.phase is Phase.AWAITING_PLAN_APPROVAL

        deadline = time.monotonic() + WAIT_S
        while not await held():
            assert time.monotonic() < deadline, "timed out waiting for the plan gate"
            await asyncio.sleep(0.05)
        await env.sleep(timedelta(hours=3))
        await handle.signal(TaskWorkflow.approve_plan, "maintainer")
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.PR_OPENED
    assert world.events("paused") == []


# ---------- the daily limit of the repo ----------
async def test_daily_waits_until_midnight(env: WorkflowEnvironment, tmp_path: Path) -> None:
    script = write_script(tmp_path, reads_then_finish(3))
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=60, daily_repo_usd=1.0)
    async with light(env, script) as world:
        # Two dollars of other tasks of the repo, spent today.
        world.costs.entries.append(
            LedgerEntry(
                "lh-earlier",
                LOCAL,
                SONNET,
                0,
                1,
                Usage(input_tokens=1_000_000),
                Decimal("2.0"),
                at=datetime.now(UTC),
            )
        )
        spec = world.spec(limits)
        handle = await begin(env, world, spec)
        await paused(world, handle)
        assert world.provider.calls == 0
        (asked,) = world.events("paused")
        assert asked["reason"] == "daily_repo_usd"
        resume_at = datetime.fromisoformat(asked["resume_at"])
        assert resume_at.hour == resume_at.minute == resume_at.second == 0
        assert resume_at.tzinfo is not None and resume_at.utcoffset() == timedelta(0)
        status = await handle.query(TaskWorkflow.status)
        assert status.paused_reason == "waiting for midnight UTC: daily repo usd"

        # Nobody signals. The durable timer ends at midnight and the task goes on.
        outcome = await result_of(handle)
        history = await handle.fetch_history()

    assert outcome.kind is OutcomeKind.PR_OPENED
    (resumed,) = world.events("resumed")
    assert resumed["via"] == "midnight"
    names = timeline(history, timers=True)
    assert "signal:add_budget" not in names
    assert names.index("timer_started") < names.index("timer_fired") < names.index("llm_turn")
    assert_checked_before_every_turn(names)


async def test_a_cancel_ends_a_task_that_waits_for_midnight(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(3))
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=60, daily_repo_usd=1.0)
    async with light(env, script) as world:
        world.costs.entries.append(
            LedgerEntry(
                "lh-earlier",
                LOCAL,
                SONNET,
                0,
                1,
                Usage(input_tokens=1_000_000),
                Decimal("2.0"),
                at=datetime.now(UTC),
            )
        )
        handle = await begin(env, world, world.spec(limits))
        await paused(world, handle)
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "tomorrow is too late"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.CANCELLED


async def test_a_daily_limit_smaller_than_one_call_ends_the_task(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(3))
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=60, daily_repo_usd=0.001)
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(limits))
        outcome = await result_of(handle)

    # Waiting for tomorrow would only bring the same answer, so the task does not sleep.
    assert outcome.kind is OutcomeKind.BUDGET_EXHAUSTED
    assert "daily budget of $0.001 for the repo is smaller than the next call" in outcome.summary
    assert world.events("paused") == [] and world.provider.calls == 0


# ---------- a task of an issue ----------
async def test_a_task_of_an_issue_asks_once_with_a_key_and_moves_the_label(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(8))
    async with light(env, script, tmp_path=tmp_path) as world:
        assert world.fake is not None and world.number is not None
        spec = world.spec(SMALL_TASK)
        handle = await begin(env, world, spec)
        await paused(world, handle)

        key = f"<!-- longhaul:key={spec.task_id}:budget:1 -->"
        assert world.fake.count_comments(REPO, world.number, containing=key) == 1
        assert NEEDS_HUMAN in world.fake.label_names(REPO, world.number)
        comment = next(c for c in world.fake.comment_bodies(REPO, world.number) if key in c)
        assert "/longhaul budget +2" in comment and "/longhaul cancel" in comment
        assert "$0.04" in comment, "the limit is in the comment"

        await top_up(handle, usd=5.0)
        await wait_until(lambda: len(world.events("resumed")) >= 1, "the resume")
        assert NEEDS_HUMAN not in world.fake.label_names(REPO, world.number)
        # The task would go on to open a pull request, and this world has no delivery.
        await handle.terminate("the test has seen what it came for")

    assert world.fake.count_comments(REPO, world.number, containing=":budget:") == 1


async def test_a_second_pause_is_another_comment_and_another_label_change(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(12))
    async with light(env, script, tmp_path=tmp_path) as world:
        assert world.fake is not None and world.number is not None
        spec = world.spec(SMALL_TASK)
        handle = await begin(env, world, spec)
        await paused(world, handle, 1)
        await top_up(handle, usd=0.001)
        await paused(world, handle, 2)
        key2 = f"<!-- longhaul:key={spec.task_id}:budget:2 -->"
        assert world.fake.count_comments(REPO, world.number, containing=key2) == 1
        assert NEEDS_HUMAN in world.fake.label_names(REPO, world.number)
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "enough"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.CANCELLED
    assert world.fake.count_comments(REPO, world.number, containing=":budget:") == 2


async def test_the_daily_pause_of_an_issue_says_so_and_asks_for_no_one(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(3))
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=60, daily_repo_usd=1.0)
    async with light(env, script, tmp_path=tmp_path) as world:
        assert world.fake is not None and world.number is not None
        world.costs.entries.append(
            LedgerEntry(
                "lh-earlier",
                REPO,
                SONNET,
                0,
                1,
                Usage(input_tokens=1_000_000),
                Decimal("2.0"),
                at=datetime.now(UTC),
            )
        )
        handle = await begin(env, world, world.spec(limits))
        await paused(world, handle)
        (comment,) = [c for c in world.fake.comment_bodies(REPO, world.number) if ":budget:1" in c]
        assert "goes on by itself" in comment and "00:00 UTC" in comment
        assert NEEDS_HUMAN not in world.fake.label_names(REPO, world.number)
        await handle.signal(TaskWorkflow.cancel, args=["maintainer", "enough"])
        outcome = await result_of(handle)

    assert outcome.kind is OutcomeKind.CANCELLED


# ---------- tasks that do not enforce budgets ----------
async def test_a_task_started_without_the_switch_makes_no_budget_check(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """A task that began before LH-702, or a local run, keeps what it always did."""
    script = write_script(tmp_path, reads_then_finish(5))
    limits = Budgets(task_usd=0.001, task_turns=2, task_wall_minutes=1, daily_repo_usd=0.001)
    async with light(env, script) as world:
        handle = await start_task(env.client, world.spec(limits), task_queue=world.queue)
        outcome = await result_of(handle)
        history = await handle.fetch_history()

    assert outcome.kind is OutcomeKind.ERROR and outcome.summary == "turn limit"
    assert "check_budget" not in timeline(history)
    assert world.events("paused") == []


# ---------- replay ----------
async def test_the_history_of_a_pause_and_of_a_midnight_wait_replay(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, reads_then_finish(8))
    async with light(env, script) as world:
        handle = await begin(env, world, world.spec(SMALL_TASK))
        await paused(world, handle)
        await top_up(handle, usd=5.0)
        await result_of(handle)
        topped_up = await handle.fetch_history()
    limits = Budgets(task_usd=50.0, task_turns=60, task_wall_minutes=60, daily_repo_usd=1.0)
    async with light(env, write_script(tmp_path, reads_then_finish(3))) as world:
        world.costs.entries.append(
            LedgerEntry(
                "lh-earlier",
                LOCAL,
                SONNET,
                0,
                1,
                Usage(input_tokens=1_000_000),
                Decimal("2.0"),
                at=datetime.now(UTC),
            )
        )
        waiting = await begin(env, world, world.spec(limits))
        await result_of(waiting)
        slept = await waiting.fetch_history()
    async with light(env, write_script(tmp_path, reads_then_finish(8))) as world:
        expired = await begin(env, world, world.spec(SMALL_TASK))
        await result_of(expired)
        ended = await expired.fetch_history()

    replayer = Replayer(
        workflows=[TaskWorkflow],
        workflow_runner=workflow_runner(),
        data_converter=guarded_data_converter,
    )
    for history in (topped_up, slept, ended):
        await replayer.replay_workflow(history)
