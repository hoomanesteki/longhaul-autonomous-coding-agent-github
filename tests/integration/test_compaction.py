"""Eviction and compaction in the workflow (LH-404), with the real activities.

A scripted coder works through a long task in a window of 8,000 tokens. The workflow runs on a
time skipping Temporal test server with the real llm_turn, compact, tool_exec and append_results
activities, a fake sandbox for the commands, the memory blob store and the memory ledger on the
checked-in prices. The compactor is scripted too: it always answers with the same notes.

What the tests read is what the activities recorded. Every llm_turn output carries the
projection of the request it sent, and the recorder holds the compaction events. The blob store
holds the log and the view.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from longhaul.context.evict import ELIDED_PREFIX
from longhaul.context.log import read_log
from longhaul.durable.activities.context import ContextActivities
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import build_system_prompt
from longhaul.agent.turns import validate_pairs
from longhaul.blobs.base import BlobStoreBase
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.context.compact import NOTES_HEADING, NOTES_TOOL_NAME
from longhaul.context.tokens import count_request_chars
from longhaul.contracts import (
    BlobRef,
    Budgets,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    ProgressNotes,
    RepoSettings,
    Role,
    Sandbox,
    TaskSpec,
    TextBlock,
    ToolContext,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.cost.ledger import MemoryLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.settings import Context
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase4]

ROOT = Path(__file__).resolve().parents[2]
PRICING = ROOT / "config" / "pricing.yaml"
BASE = "b" * 40
WAIT_S = 120.0
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

WINDOW = 8_000
CONTEXT = Context(
    window_tokens=WINDOW,
    compact_at=0.6,
    hard_limit=0.85,
    keep_recent_turns=4,
    elide_tool_outputs_after_turns=2,
    # Small enough that a few results fit in the window, and big enough that the commands
    # below print their lines whole.
    tool_output_max_tokens=250,
)
HARD_LIMIT_TOKENS = int(CONTEXT.hard_limit * WINDOW)
NOTES = ProgressNotes(
    goal="Fix every module of the calc package",
    plan_status=[],
    files_touched=["calc/add.py", "calc/sub.py"],
    decisions=["Fix one module at a time and run its tests"],
    failing_tests=[],
    open_hypotheses=[],
    tried_and_failed=["Rewriting calc/__init__.py broke the imports"],
    covers_turns_up_to=0,
)
SAID = (
    "I read the module, found the line that is wrong and changed it. The tests for this module "
    "now pass, so I move on to the next one. "
)


def config_for(role: Role) -> LoopConfig:
    return LoopConfig(
        model=HAIKU if role is Role.COMPACTOR else SONNET,
        max_turns=200,
        max_tokens_per_turn=1000,
        max_tokens_retry=5000,
    )


# ---------- the script ----------
def command(turn: int) -> str:
    """A command that prints 14 numbered lines, so every turn has an output of its own."""
    return f"printf 'turn {turn} line %s: output of the test run\\n' $(seq 1 14)"


def printed(turn: int) -> str:
    return "\n".join(f"turn {turn} line {n}: output of the test run" for n in range(1, 15))


def result_text(turn: int) -> str:
    """What run_command answers: the exit code, then the output without its last newline."""
    return f"exit code: 0\nstdout:\n{printed(turn)}"


def coder_turn(turn: int, *, marker: bool | None = None) -> dict[str, Any]:
    match: dict[str, Any] = {"role": "coder", "turn_index": turn}
    if marker:
        match["request_contains"] = "turn_offset="
    return {
        "match": match,
        "respond": {
            "text": f"Turn {turn}. {SAID * 4}",
            "tool_calls": [{"name": "run_command", "input": {"command": command(turn)}}],
        },
    }


def finish_turn(turn: int) -> dict[str, Any]:
    return {
        "match": {"role": "coder", "turn_index": turn},
        "respond": {"tool_calls": [{"name": "finish", "input": {"summary": "Fixed them all."}}]},
    }


def compactor_turn() -> dict[str, Any]:
    call = {"name": NOTES_TOOL_NAME, "input": NOTES.model_dump(mode="json")}
    return {"match": {"role": "compactor"}, "respond": {"tool_calls": [call]}}


def write_script(tmp_path: Path, steps: Sequence[dict[str, Any]]) -> Path:
    path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump({"steps": list(steps)}))
    return path


def long_task(turns: int) -> list[dict[str, Any]]:
    return [*(coder_turn(t) for t in range(turns - 1)), finish_turn(turns - 1), compactor_turn()]


# ---------- the worker ----------
class RecordingProvider(ScriptedProvider):
    """A scripted provider that keeps what each call was asked, and how many it failed."""

    def __init__(self, script: Path) -> None:
        super().__init__(script)
        self.requests: list[dict[str, Any]] = []
        self.failures = 0

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.requests.append({k: kwargs[k] for k in ("model", "system", "messages")})
        try:
            return await super().complete(**kwargs)
        except Exception:
            self.failures += 1
            raise

    def of(self, model: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["model"] == model]


class SpyLlm(LlmActivities):
    """llm_turn that keeps its outputs, and the refusals before the call."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.outputs: list[acts.LlmTurnOutput] = []
        self.refused = 0

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        try:
            out = await super().llm_turn(inp)
        except ApplicationError as exc:
            if exc.type == "context_too_long":
                self.refused += 1
            raise
        self.outputs.append(out)
        return out


class SpyTools(ToolActivities):
    """append_results that keeps the view each turn ended with."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.views: list[BlobRef] = []

    @activity.defn(name=acts.APPEND_RESULTS.name)
    async def append_results(self, inp: acts.AppendResultsInput) -> BlobRef:
        ref = await super().append_results(inp)
        self.views.append(ref)
        return ref


class Boxes:
    """prepare_workspace and cleanup for the tests, with one fake sandbox per task."""

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

    async def destroy_all(self) -> None:
        for box in self.boxes.values():
            await box.destroy()


@dataclass
class Run:
    queue: str
    provider: RecordingProvider
    llm: SpyLlm
    tools: SpyTools
    blobs: BlobStoreBase
    recorder: MemoryRecorder
    ledger: MemoryLedger
    registry: ToolRegistry
    boxes: Boxes

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.recorder.events if e.type == event_type]

    async def view(self, ref: BlobRef | None = None) -> list[Message]:
        return await self.blobs.get_json(ref or self.tools.views[-1], list[Message])


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def make_spec() -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{uuid.uuid4().hex[:8]}",
        repo="acme/calc",
        issue_number=42,
        title="Every module of calc is wrong",
        body="Fix each module of the calc package and run its tests.",
        settings=RepoSettings(repo="acme/calc", budgets=Budgets(task_turns=200)),
    )


@asynccontextmanager
async def running(
    env: WorkflowEnvironment, script: Path, *, policy: Context = CONTEXT
) -> AsyncIterator[Run]:
    """A worker with the real activities. Yields what the tests look at."""
    blobs = MemoryBlobStore()
    provider = RecordingProvider(script)
    recorder = MemoryRecorder()
    ledger = MemoryLedger(Pricing.load(PRICING))
    registry = default_registry(blobs=blobs)
    boxes = Boxes()
    llm = SpyLlm(
        provider, blobs, recorder, registry.specs(), config_for, ledger=ledger, context=policy
    )
    compactor = ContextActivities(
        provider, blobs, registry.specs(), config_for, policy, ledger=ledger
    )
    tools = SpyTools(
        registry,
        boxes,
        blobs,
        recorder,
        checkpoints=NoCheckpoints(),
        tool_output_max_tokens=policy.tool_output_max_tokens,
    )
    events = EventActivities(recorder)
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *compactor.activities,
                *tools.activities,
                *events.activities,
                boxes.prepare_workspace,
                boxes.cleanup,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield Run(queue, provider, llm, tools, blobs, recorder, ledger, registry, boxes)
    finally:
        await boxes.destroy_all()


async def finished(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    return await asyncio.wait_for(handle.result(), WAIT_S)


async def play(env: WorkflowEnvironment, script: Path, spec: TaskSpec) -> tuple[Run, Outcome]:
    """Run the task to its end. The Run is still usable: it only holds the stores."""
    async with running(env, script) as run:
        handle = await start_task(env.client, spec, task_queue=run.queue)
        outcome = await finished(handle)
        return run, outcome


# ---------- LH-404 acceptance criteria ----------
async def test_never_exceeds_hard_limit(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """Fifty turns in a window of 8,000 tokens, and no request goes over 85 percent of it.

    The assertion is on the projections the activity recorded for each request it sent. Without
    the two tiers the same task would have filled the window many times over, which the log
    shows.
    """
    spec = make_spec()
    run, outcome = await play(env, write_script(tmp_path, long_task(50)), spec)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Fixed them all.")
    projections = [out.request_tokens for out in run.llm.outputs]
    assert len(projections) == 50
    assert all(tokens > 0 for tokens in projections)
    assert max(projections) <= HARD_LIMIT_TOKENS

    # The window was crossed several times over: eviction and compaction are what held it.
    system = build_system_prompt(Role.CODER, spec)
    log = await read_log(run.blobs, spec.task_id)
    whole = count_request_chars(system, log, run.registry.specs()) / 3.5
    assert whole > 2 * WINDOW

    # Compaction ran more than once, and the guard in front of the call never had to step in.
    compactions = run.events("compaction")
    assert len(compactions) >= 3
    assert run.llm.refused == 0
    assert all(c["forced"] is False for c in compactions)
    assert all(c["tokens_after"] < c["tokens_before"] for c in compactions)
    assert all(c["tokens_before"] < WINDOW for c in compactions)


async def test_forced_compaction_on_overflow(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """The provider says the prompt is too long. The view is compacted to two turns and the
    turn is asked once more. The run goes on to its end."""
    steps = [
        # After a compaction the request carries the turn counter of the notes, and this step
        # answers. Before it the request has none, and the next step refuses it.
        coder_turn(6, marker=True),
        {"match": {"role": "coder", "turn_index": 6}, "respond": {"raise": "context_too_long"}},
        *(coder_turn(t) for t in range(9) if t != 6),
        finish_turn(9),
        compactor_turn(),
    ]
    spec = make_spec()
    run, outcome = await play(env, write_script(tmp_path, steps), spec)

    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Fixed them all.")
    assert run.provider.failures == 1
    asked = run.provider.of(SONNET)
    assert len(asked) == 11  # ten turns, and turn 6 twice

    # One forced compaction, on the turn that was refused.
    (event,) = run.events("compaction")
    assert event["forced"] is True
    assert event["turn_index"] == 6
    assert event["status"] == "ok"
    assert event["tokens_after"] < event["tokens_before"]

    # The refused request had six turns. The retry had two, behind the notes.
    refused, retried = asked[6], asked[7]
    assert sum(m.role == "assistant" for m in refused["messages"]) == 6
    assert sum(m.role == "assistant" for m in retried["messages"]) == acts.FORCED_KEEP_RECENT_TURNS
    assert NOTES_HEADING in text_of(retried["messages"][0])

    # The compactor was paid for, and the view of the end of the run still pairs up.
    assert [(e.model, e.turn_index) for e in run.ledger.entries if e.model == HAIKU] == [(HAIKU, 6)]
    validate_pairs(await run.view())


async def test_log_and_view_split(env: WorkflowEnvironment, tmp_path: Path) -> None:
    """The log holds every turn whole. The view holds the stubs and the notes."""
    spec = make_spec()
    run, _ = await play(env, write_script(tmp_path, long_task(40)), spec)

    log = await read_log(run.blobs, spec.task_id)
    view = await run.view()

    # The log: forty replies, every output whole, no stub, no notes, and a valid transcript.
    assert sum(m.role == "assistant" for m in log) == 40
    validate_pairs(log)
    assert ELIDED_PREFIX not in text_of(log)
    assert NOTES_HEADING not in text_of(log)
    assert result_text(0) in text_of(log[:3])  # the first output, in full
    assert result_text(30) in text_of(log)

    # The view: compacted, so it starts with the notes and has fewer turns than the log.
    assert NOTES_HEADING in text_of(view[:1])
    assert sum(m.role == "assistant" for m in view) < 40
    validate_pairs(view)
    # Its older results are stubs, and the latest are whole.
    outputs = [b.content for m in view for b in m.content if isinstance(b, ToolResultBlock)]
    assert any(o.startswith(ELIDED_PREFIX) for o in outputs)
    assert outputs[-2] == result_text(38)  # the last result is the answer to finish
    assert result_text(30) not in text_of(view)

    # The log is the same record in order: every reply of the view is in it.
    replies = [m for m in log if m.role == "assistant"]
    kept = [m for m in view if m.role == "assistant"]
    assert kept == replies[-len(kept) :]


async def test_an_elided_handle_still_reads_the_whole_output(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    spec = make_spec()
    run, _ = await play(env, write_script(tmp_path, long_task(12)), spec)

    view = await run.view()
    stubs = [
        b.content
        for m in view
        for b in m.content
        if isinstance(b, ToolResultBlock) and b.content.startswith(ELIDED_PREFIX)
    ]
    assert stubs, "the view has no stub"
    first = stubs[0]
    assert first.endswith(", handle out_" + first.rsplit("out_", 1)[1])
    handle = "out_" + first.rsplit("out_", 1)[1].removesuffix("]")

    read_output = run.registry.get("read_output")
    assert read_output is not None
    box = FakeSandbox("unused")
    try:
        result = await read_output.run(
            box,
            {"handle": handle},
            ToolContext(task_id=spec.task_id, turn_index=0, blob_prefix="", output_max_tokens=4000),
        )
    finally:
        await box.destroy()
    assert result.ok
    assert "line 14: output of the test run" in result.output


async def test_the_notes_reach_the_next_request_and_the_system_prompt_stays(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    spec = make_spec()
    run, _ = await play(env, write_script(tmp_path, long_task(40)), spec)

    coder = run.provider.of(SONNET)
    compacted = [r for r in coder if NOTES_HEADING in text_of(r["messages"][:1])]
    assert compacted, "no request carried the notes"
    first = text_of(compacted[0]["messages"][:1])
    assert NOTES.goal in first
    assert NOTES.tried_and_failed[0] in first
    # D7: the system prompt is the same bytes on every call, before and after a compaction.
    assert {r["system"] for r in coder} == {build_system_prompt(Role.CODER, spec)}
    # The notes come after the first message, in the same message, and not in the prompt.
    assert NOTES_HEADING not in coder[-1]["system"]


async def test_every_compactor_call_has_a_ledger_row_of_its_own(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    spec = make_spec()
    run, _ = await play(env, write_script(tmp_path, long_task(40)), spec)

    compactions = run.events("compaction")
    rows = [e for e in run.ledger.entries if e.model == HAIKU]
    assert len(rows) == len(compactions) >= 2
    assert len(run.provider.of(HAIKU)) == len(rows)
    assert all(e.usage.input_tokens > 0 and e.usd > 0 for e in rows)
    assert {e.task_id for e in run.ledger.entries} == {spec.task_id}


def text_of(messages: Sequence[Message]) -> str:
    """The text of the messages and the results in them, in one string."""
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolResultBlock):
                parts.append(block.content)
            elif isinstance(block, ToolUseBlock):
                parts.append(str(block.input))
    return "\n".join(parts)
