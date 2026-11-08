"""The summarize_for_human activity (LH-506): the question for a person, from the compactor model.

It runs inside temporalio's ActivityEnvironment with a fake provider, the memory blob store and
the memory ledger on the checked-in prices. The comment and the ledger are the parts that
matter: the comment has all four sections and fits in 250 words, and every billed call has a
ledger row of its own (invariant I6).
"""

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import FIRST_USER_MESSAGE
from longhaul.blobs.base import blob_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.context.human_summary import SUMMARY_TOOL_NAME
from longhaul.contracts import (
    BlobRef,
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    RepoSettings,
    Role,
    StuckReason,
    TaskSpec,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from longhaul.cost.ledger import MemoryLedger
from longhaul.cost.pricing import Pricing
from longhaul.durable import acts
from longhaul.durable.activities.context import ContextActivities
from longhaul.settings import Context
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

ROOT = Path(__file__).resolve().parents[2]
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
PRICES = Pricing.load(ROOT / "config" / "pricing.yaml")
SPEC = TaskSpec(
    task_id="lh-acme-calc-42",
    repo="acme/calc",
    issue_number=42,
    title="add() subtracts",
    body="add(2, 3) returns -1",
    settings=RepoSettings(repo="acme/calc"),
)
POLICY = Context(
    window_tokens=8_000,
    compact_at=0.6,
    hard_limit=0.85,
    keep_recent_turns=3,
    elide_tool_outputs_after_turns=2,
    tool_output_max_tokens=400,
)
FIRST = Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])
USAGE = Usage(input_tokens=900, output_tokens=120)
GOOD: dict[str, Any] = {
    "goal": "Make add() return the sum of its arguments.",
    "tried": ["Changed - to +.", "Ran pytest -q three times.", "Read tests/test_calc.py."],
    "guess": "Another copy of calc is imported before src/calc.",
    "question": "Which copy of calc should the tests import?",
}


def config_for(role: Role) -> LoopConfig:
    return LoopConfig(model=HAIKU if role is Role.COMPACTOR else SONNET)


def reply(arguments: dict[str, Any] | None, usage: Usage = USAGE) -> LLMResponse:
    if arguments is None:
        return LLMResponse(
            content=[TextBlock(text="No idea.")],
            tool_calls=[],
            stop_reason="end_turn",
            usage=usage,
            model=HAIKU,
            latency_ms=1,
        )
    block = ToolUseBlock(id="s1", name=SUMMARY_TOOL_NAME, input=arguments)
    return LLMResponse(
        content=[block],
        tool_calls=[ToolCall(id=block.id, name=block.name, input=block.input)],
        stop_reason="tool_use",
        usage=usage,
        model=HAIKU,
        latency_ms=1,
    )


class FakeProvider:
    name = "fake"

    def __init__(self, *steps: LLMResponse | ProviderError) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, ProviderError):
            raise step
        return step

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


class Rig:
    def __init__(self, *steps: LLMResponse | ProviderError) -> None:
        self.provider = FakeProvider(*steps)
        self.blobs = MemoryBlobStore()
        self.ledger = MemoryLedger(PRICES)
        self.activities = ContextActivities(
            self.provider,
            self.blobs,
            default_registry().specs(),
            config_for,
            POLICY,
            ledger=self.ledger,
            templates=ROOT / "templates",
        )

    async def run(self, inp: acts.SummarizeInput, *, attempt: int = 1) -> acts.SummarizeOutput:
        env = ActivityEnvironment()
        env.info = dataclasses.replace(env.info, attempt=attempt)
        return await env.run(self.activities.summarize_for_human, inp)

    async def transcript(self) -> BlobRef:
        messages = [FIRST]
        for n in range(1, 4):
            call = ToolUseBlock(id=f"c{n}", name="run_tests", input={})
            result = ToolResultBlock(tool_use_id=f"c{n}", content=f"FAILED run {n}", is_error=True)
            messages += [
                Message(role="assistant", content=[call]),
                Message(role="user", content=[result]),
            ]
        key = blob_key(SPEC.task_id, "turns", 0, "transcript-start.json")
        return await self.blobs.put_json(messages, key)


def summarize_input(ref: BlobRef | None, **fields: Any) -> acts.SummarizeInput:
    values: dict[str, Any] = {
        "task_id": SPEC.task_id,
        "turn_index": 2,
        "spec": SPEC,
        "transcript_ref": ref,
        "reason": StuckReason.REPEATED_FAILURE,
        "evidence": ["run_tests failed 3 times with the same E assert -1 == 5"],
        "pause": 1,
    }
    return acts.SummarizeInput(**{**values, **fields})


# ---------- the comment ----------
async def test_the_comment_has_all_four_sections_and_fits_in_250_words() -> None:
    rig = Rig(reply(GOOD))
    out = await rig.run(summarize_input(await rig.transcript()))

    for heading in ("What I'm trying to do", "What I tried", "My current guess", "Question"):
        assert f"### {heading}" in out.comment
    assert "Which copy of calc should the tests import?" in out.comment
    assert len(out.comment.split()) < 250
    assert out.question == GOOD["question"]
    assert out.status == "ok"


async def test_the_compactor_model_answers_and_sees_the_latest_turns() -> None:
    rig = Rig(reply(GOOD))
    await rig.run(summarize_input(await rig.transcript()))

    (sent,) = rig.provider.calls
    assert sent["model"] == HAIKU
    request = sent["messages"][0].content[0].text
    assert "Why the agent is stuck" in request
    assert "run_tests failed 3 times" in request
    assert "FAILED run 3" in request
    assert "add() subtracts" in request


async def test_a_model_that_gives_nothing_usable_still_gives_a_comment() -> None:
    rig = Rig(reply(None), reply(None))
    out = await rig.run(summarize_input(await rig.transcript()))

    assert out.status == "fallback"
    assert "### Question" in out.comment
    assert "run_tests failed 3 times" in out.comment


async def test_a_task_with_no_transcript_yet_can_still_ask() -> None:
    rig = Rig(reply(GOOD))
    out = await rig.run(summarize_input(None))
    assert out.status == "ok"


# ---------- the ledger ----------
async def test_every_billed_call_has_a_ledger_row_and_the_cost_is_their_sum() -> None:
    rig = Rig(reply(None, Usage(input_tokens=500, output_tokens=40)), reply(GOOD))
    out = await rig.run(summarize_input(await rig.transcript()), attempt=2)

    rows = rig.ledger.entries
    assert [(r.task_id, r.model, r.turn_index, r.attempt) for r in rows] == [
        (SPEC.task_id, HAIKU, 2, 2),
        (SPEC.task_id, HAIKU, 2, 2),
    ]
    assert out.status == "repaired"
    assert out.cost_usd == pytest.approx(float(sum(r.usd for r in rows)))
    assert out.cost_usd > 0


# ---------- failures ----------
async def test_a_provider_error_fails_the_attempt_with_its_kind() -> None:
    rig = Rig(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))
    with pytest.raises(ApplicationError) as caught:
        await rig.run(summarize_input(await rig.transcript()))
    assert caught.value.type == ProviderErrorKind.OVERLOADED.value


def test_the_activity_is_registered_under_the_name_the_workflow_calls() -> None:
    rig = Rig()
    assert rig.activities.summarize_for_human in rig.activities.activities
    assert acts.SUMMARIZE_FOR_HUMAN in acts.ALL
