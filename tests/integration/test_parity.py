"""The in-process loop and the durable workflow must agree on the same script.

Both are driven by the rules in agent/rules.py, but the workflow applies them across activity
boundaries, with the transcript in a blob store and every call made through Temporal. A rule
that was copied wrongly, or a transcript edit that the activities do differently, shows up as a
difference here. Each scenario runs one of the scripts of tests/fixtures/scripts twice: through
run_loop with a fake sandbox, and through TaskWorkflow with the real activities and a second fake
sandbox. The outcome, the number of turns and the final transcript must match.

Two things are normalized before the transcripts are compared. Tool ids are renumbered in order
of appearance, and the run time that pytest prints is blanked. A scripted tool id is a hash of
the whole request, and the request holds pytest's timing line, so two runs of a script that
runs pytest get different ids even when nothing else differs.
"""

import asyncio
import re
import shlex
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig, LoopResult, run_loop
from longhaul.agent.prompts import FIRST_USER_MESSAGE
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    CallOptions,
    LLMProvider,
    LLMResponse,
    Message,
    Outcome,
    RepoSettings,
    Sandbox,
    TaskSpec,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import ToolActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
SCRIPTS = FIXTURES / "scripts"
REPOS = FIXTURES / "repos"
SKIP_PARTS = {"REFERENCE.patch", "__pycache__", ".pytest_cache"}
TEST_COMMAND = f"{shlex.quote(sys.executable)} -m pytest -q"
WAIT_S = 60.0
BASE = "b" * 40

CFG = LoopConfig(
    model="test-model",
    max_turns=10,
    max_tokens_per_turn=1000,
    max_tokens_retry=5000,
    tool_output_max_tokens=4000,
)


# ---------- the matrix ----------
@dataclass(frozen=True)
class Scenario:
    script: str
    seed: str | None = None  # a fixture repo copied into the sandbox before the run
    max_turns: int = 10
    # With this set the provider answers from `script` below the retry cap and from `retry`
    # at or above it, because a scripted reply depends on the request alone and a retry of a
    # cut turn is the same request with a higher cap.
    retry: str | None = None

    @property
    def id(self) -> str:
        name = self.script.removesuffix(".yaml")
        return f"{name}+retry" if self.retry else name


SCENARIOS = [
    Scenario("calc_fix.yaml", seed="calc"),
    Scenario("loop_nudge.yaml"),
    Scenario("loop_always_idle.yaml"),
    Scenario("loop_idle_twice_apart.yaml"),
    Scenario("loop_empty_first.yaml"),
    Scenario("loop_empty_after_tools.yaml"),
    Scenario("loop_bad_finish.yaml"),
    Scenario("loop_finish_midway.yaml", seed="calc"),
    Scenario("loop_tool_error.yaml"),
    Scenario("loop_never_finishes.yaml", max_turns=3),
    Scenario("loop_max_tokens_cut.yaml"),
    Scenario("loop_max_tokens_cut.yaml", retry="loop_max_tokens_retry.yaml"),
    Scenario("loop_refusal.yaml"),
    Scenario("loop_refusal_category_only.yaml"),
    Scenario("loop_refusal_bare.yaml"),
    Scenario("loop_context_window.yaml"),
    Scenario("loop_context_too_long.yaml"),
    Scenario("loop_unexpected_stop.yaml"),
    Scenario("loop_pause_turn.yaml"),
    Scenario("loop_pause_forever.yaml"),
]

# Scripts that are left out, and why.
LEFT_OUT = {
    # The in-process loop has no retry policy, so the error is the caller's. In the workflow
    # Temporal retries it; tests/integration/test_activities.py covers that.
    "loop_overloaded.yaml": "provider errors are retried by Temporal, not by the loop",
    # It checks that long arguments are cut in the printed event, which only the loop prints.
    "loop_long_command.yaml": "event presentation, which only the in-process loop has",
    # It is the second half of the max_tokens retry scenario.
    "loop_max_tokens_retry.yaml": "used as the retry script of loop_max_tokens_cut.yaml",
}


def test_the_matrix_covers_every_loop_script() -> None:
    # A script added for the loop should be run through the workflow as well, or be named above.
    used = {s.script for s in SCENARIOS} | {s.retry for s in SCENARIOS if s.retry}
    on_disk = {p.name for p in SCRIPTS.glob("loop_*.yaml")} | {"calc_fix.yaml"}
    assert on_disk - used - set(LEFT_OUT) == set(), "a loop script is not in the parity matrix"
    assert set(LEFT_OUT) <= on_disk


# ---------- providers ----------
class CapRouter:
    """Answers from one script below a max_tokens value and from another at or above it."""

    name = "cap-router"

    def __init__(self, low: ScriptedProvider, high: ScriptedProvider, cap: int) -> None:
        self.low, self.high, self.cap = low, high, cap

    async def complete(self, **kwargs: Any) -> LLMResponse:
        target = self.high if kwargs["max_tokens"] >= self.cap else self.low
        return await target.complete(**kwargs)

    async def count_tokens(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        options: CallOptions | None = None,
    ) -> int:
        return 0


def provider_for(scenario: Scenario) -> LLMProvider:
    low = ScriptedProvider(SCRIPTS / scenario.script)
    if scenario.retry is None:
        return low
    return CapRouter(low, ScriptedProvider(SCRIPTS / scenario.retry), CFG.max_tokens_retry)  # type: ignore[return-value]


# ---------- the sandbox ----------
async def seeded(name: str | None, sandbox_id: str = "fake") -> FakeSandbox:
    box = FakeSandbox(sandbox_id)
    if name is None:
        return box
    source = REPOS / name
    for f in sorted(source.rglob("*")):
        parts = set(f.relative_to(source).parts)
        if (
            f.is_file()
            and not parts & SKIP_PARTS
            and not any(p.endswith(".egg-info") for p in parts)
        ):
            await box.write_file(str(f.relative_to(source)), f.read_bytes())
    return box


def spec_for(scenario: Scenario) -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-parity-{uuid.uuid4().hex[:8]}",
        repo="local:/calc",
        issue_number=None,
        title="add() returns the difference",
        body="`calc.add` subtracts its arguments instead of adding them.",
        settings=RepoSettings(
            repo="local:/calc",
            test_command=TEST_COMMAND,
            budgets=Budgets(task_turns=scenario.max_turns),
        ),
    )


# ---------- the two runs ----------
async def through_the_loop(scenario: Scenario, spec: TaskSpec) -> LoopResult:
    box = await seeded(scenario.seed)
    try:
        return await run_loop(
            spec,
            provider_for(scenario),
            default_registry(test_command=TEST_COMMAND),
            box,
            CFG.model_copy(update={"max_turns": scenario.max_turns}),
        )
    finally:
        await box.destroy()


class SpyLlm(LlmActivities):
    """LlmActivities that remembers each transcript it hands the workflow."""

    def __init__(self, *args: Any, refs: list[BlobRef], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.refs = refs

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        out = await super().llm_turn(inp)
        self.refs.append(out.transcript_ref)
        return out


class SpyTools(ToolActivities):
    def __init__(self, *args: Any, refs: list[BlobRef], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.refs = refs

    @activity.defn(name=acts.APPEND_RESULTS.name)
    async def append_results(self, inp: acts.AppendResultsInput) -> BlobRef:
        ref = await super().append_results(inp)
        self.refs.append(ref)
        return ref


class Workspace:
    def __init__(self, box: Sandbox) -> None:
        self.box = box

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        return acts.PrepareWorkspaceOutput(sandbox_id=self.box.id, base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        return None


class OneBox:
    def __init__(self, box: Sandbox) -> None:
        self.box = box

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.box if sandbox_id == self.box.id else None


@dataclass(frozen=True)
class Seen:
    outcome: Outcome
    turns: int
    transcript: list[Message]


async def through_the_workflow(
    env: WorkflowEnvironment, scenario: Scenario, spec: TaskSpec
) -> Seen:
    box = await seeded(scenario.seed)
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    registry = default_registry(test_command=TEST_COMMAND)
    refs: list[BlobRef] = []
    llm = SpyLlm(
        provider_for(scenario), blobs, recorder, registry.specs(), lambda role: CFG, refs=refs
    )
    tools = SpyTools(registry, OneBox(box), blobs, recorder, refs=refs)
    workspace = Workspace(box)
    queue = f"lh-parity-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *tools.activities,
                workspace.prepare_workspace,
                workspace.cleanup,
            ],
            workflow_runner=workflow_runner(),
        ):
            handle = await start_task(env.client, spec, task_queue=queue)
            outcome = await asyncio.wait_for(handle.result(), WAIT_S)
            status = await handle.query(TaskWorkflow.status)
    finally:
        await box.destroy()
    # A call that fails before it stores anything leaves no transcript. The loop would still
    # hold the first message, so that is what the workflow's run is compared with.
    transcript = (
        await blobs.get_json(refs[-1], list[Message])
        if refs
        else [Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])]
    )
    return Seen(outcome, status.turn_index, transcript)


# ---------- normalizing ----------
_RUN_TIME = re.compile(r" in \d+(?:\.\d+)?s\b(?: \([^)]*\))?")


def normalized(transcript: list[Message]) -> list[dict[str, Any]]:
    ids: dict[str, str] = {}

    def rename(tool_id: str) -> str:
        return ids.setdefault(tool_id, f"call-{len(ids)}")

    out: list[dict[str, Any]] = []
    for message in transcript:
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            dumped = block.model_dump(mode="json")
            if dumped["type"] == "tool_use":
                dumped["id"] = rename(dumped["id"])
            elif dumped["type"] == "tool_result":
                dumped["tool_use_id"] = rename(dumped["tool_use_id"])
                dumped["content"] = _RUN_TIME.sub(" in <time>", dumped["content"])
            blocks.append(dumped)
        out.append({"role": message.role, "content": blocks})
    return out


# ---------- the tests ----------
@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


@pytest.mark.asyncio(loop_scope="module")
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.id)
async def test_the_workflow_agrees_with_the_in_process_loop(
    env: WorkflowEnvironment, scenario: Scenario
) -> None:
    spec = spec_for(scenario)
    expected = await through_the_loop(scenario, spec)
    actual = await through_the_workflow(env, scenario, spec)

    assert actual.outcome == expected.outcome
    assert actual.turns == expected.turns
    assert normalized(actual.transcript) == normalized(expected.transcript)


def test_normalizing_renumbers_ids_and_blanks_the_run_time() -> None:
    def transcript(tool_id: str, took: str) -> list[Message]:
        return [
            Message(
                role="assistant", content=[ToolUseBlock(id=tool_id, name="run_tests", input={})]
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id=tool_id, content=f"3 passed in {took}")],
            ),
        ]

    assert normalized(transcript("toolu_a", "0.02s")) == normalized(transcript("toolu_b", "1.50s"))
    assert normalized(transcript("toolu_a", "0.02s")) != normalized(
        [*transcript("toolu_a", "0.02s"), Message(role="user", content=[TextBlock(text="x")])]
    )
