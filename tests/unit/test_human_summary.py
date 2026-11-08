"""The question for a person (LH-506): what the agent was doing, what it tried, a guess, a question.

Nothing here calls a model. summarize() runs against a fake provider that answers from a list
and keeps every request. The rules are those of compact(): a tool the model fills in, one repair
turn, a fallback that needs no model, and a row on the ledger for every call that was billed.
"""

from typing import Any

import pytest
from longhaul.context.human_summary import (
    SUMMARY_TOOL_NAME,
    StuckSummary,
    build_request,
    fallback_summary,
    summarize,
)

from longhaul.context import human_summary
from longhaul.contracts import (
    CallOptions,
    LLMResponse,
    ProgressNotes,
    ProviderError,
    ProviderErrorKind,
    StuckReason,
    TextBlock,
    ToolCall,
    ToolUseBlock,
    Usage,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

USAGE = Usage(input_tokens=700, output_tokens=90)
GOOD: dict[str, Any] = {
    "goal": "Make add() return the sum.",
    "tried": ["Changed - to +", "Ran pytest -q", "Read the test"],
    "guess": "Another copy of calc is imported first.",
    "question": "Which copy of calc should the tests import?",
}


def reply(arguments: dict[str, Any] | None, usage: Usage = USAGE, call_id: str = "c1") -> Any:
    if arguments is None:
        return LLMResponse(
            content=[TextBlock(text="I am not sure.")],
            tool_calls=[],
            stop_reason="end_turn",
            usage=usage,
            model="m",
            latency_ms=1,
        )
    block = ToolUseBlock(id=call_id, name=SUMMARY_TOOL_NAME, input=arguments)
    return LLMResponse(
        content=[block],
        tool_calls=[ToolCall(id=block.id, name=block.name, input=block.input)],
        stop_reason="tool_use",
        usage=usage,
        model="m",
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


FALLBACK = fallback_summary("add() subtracts", StuckReason.REPEATED_FAILURE, ["pytest failed 3x"])


async def ask(provider: FakeProvider, billed: list[Usage] | None = None) -> Any:
    async def on_billed(usage: Usage) -> None:
        if billed is not None:
            billed.append(usage)

    return await summarize(provider, "m", "the request", FALLBACK, on_billed=on_billed)


# ---------- the answer ----------
async def test_a_valid_call_is_the_summary() -> None:
    provider = FakeProvider(reply(GOOD))
    got = await ask(provider)
    assert got.status == "ok"
    assert got.summary == StuckSummary.model_validate(GOOD)
    (sent,) = provider.calls
    assert sent["options"] == CallOptions(effort=None, force_tool=SUMMARY_TOOL_NAME)
    assert [t.name for t in sent["tools"]] == [SUMMARY_TOOL_NAME]
    assert sent["system"].startswith("Role: compactor")


async def test_a_reply_without_the_tool_gets_one_repair_turn() -> None:
    provider = FakeProvider(reply(None), reply(GOOD))
    got = await ask(provider)
    assert got.status == "repaired"
    assert got.summary.goal == GOOD["goal"]
    assert len(provider.calls) == 2
    assert human_summary.REPAIR_PREFIX in provider.calls[1]["messages"][-1].content[-1].text


async def test_a_blank_question_is_not_valid() -> None:
    bad = {**GOOD, "question": "  "}
    provider = FakeProvider(reply(bad), reply(GOOD))
    got = await ask(provider)
    assert got.status == "repaired"


async def test_two_bad_replies_give_the_fallback_without_another_call() -> None:
    provider = FakeProvider(reply(None), reply({"goal": "x"}))
    got = await ask(provider)
    assert got.status == "fallback"
    assert got.summary == FALLBACK
    assert len(provider.calls) == 2


async def test_every_billed_call_is_reported_on_its_own() -> None:
    first, second = Usage(input_tokens=10, output_tokens=1), Usage(input_tokens=20, output_tokens=2)
    billed: list[Usage] = []
    await ask(FakeProvider(reply(None, first), reply(GOOD, second)), billed)
    assert billed == [first, second]


async def test_a_provider_that_refuses_the_forced_tool_is_asked_again_on_auto() -> None:
    refused = ProviderError(ProviderErrorKind.INVALID_REQUEST, "force_tool is not supported")
    provider = FakeProvider(refused, reply(GOOD))
    got = await ask(provider)
    assert got.status == "ok"
    assert [c["options"].force_tool for c in provider.calls] == [SUMMARY_TOOL_NAME, None]


async def test_another_provider_error_goes_up_for_the_retry_of_the_activity() -> None:
    provider = FakeProvider(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))
    with pytest.raises(ProviderError) as caught:
        await ask(provider)
    assert caught.value.kind is ProviderErrorKind.OVERLOADED


# ---------- the request ----------
def test_the_request_names_the_reason_the_evidence_and_the_latest_turns() -> None:
    text = build_request(
        task="Issue #42: add() subtracts",
        reason=StuckReason.REPEATED_FAILURE,
        evidence=["pytest -q failed 3 times with the same E assert -1 == 5"],
        notes=None,
        recent="[turn 3]\n  call run_tests {}",
    )
    assert "Why the agent is stuck" in text
    assert "repeated_failure" in text
    assert "pytest -q failed 3 times" in text
    assert "Issue #42: add() subtracts" in text
    assert "[turn 3]" in text
    assert SUMMARY_TOOL_NAME in text


def test_a_question_of_the_agent_is_quoted_in_the_request() -> None:
    text = build_request(
        task="t",
        reason=StuckReason.AGENT_ASKED,
        evidence=["Should add() take floats?"],
        notes=None,
        recent="",
    )
    assert "The agent asked a person: Should add() take floats?" in text


def test_the_notes_of_an_earlier_compaction_are_in_the_request() -> None:
    notes = ProgressNotes(
        goal="Fix add()",
        plan_status=[],
        files_touched=[],
        decisions=[],
        failing_tests=[],
        open_hypotheses=[],
        tried_and_failed=["casting to int"],
        covers_turns_up_to=4,
    )
    text = build_request(
        task="t", reason=StuckReason.NO_PROGRESS, evidence=[], notes=notes, recent=""
    )
    assert "casting to int" in text


def test_the_latest_turns_are_cut_from_the_front_when_they_are_long() -> None:
    text = build_request(
        task="t",
        reason=StuckReason.NO_PROGRESS,
        evidence=[],
        notes=None,
        recent="old " * 20_000 + "THE END",
    )
    assert "THE END" in text
    assert len(text) < human_summary.MAX_RECENT_CHARS + 5_000


# ---------- the fallback ----------
def test_the_fallback_needs_no_model_and_is_a_valid_summary() -> None:
    got = fallback_summary(
        "add() subtracts", StuckReason.EDIT_CHURN, ["a.py lines 1-4 edited 4 times"] * 9
    )
    assert StuckSummary.model_validate(got.model_dump()) == got
    assert 1 <= len(got.tried) <= human_summary.MAX_TRIED
    assert "add() subtracts" in got.goal


def test_the_fallback_of_a_question_asks_that_question() -> None:
    got = fallback_summary("t", StuckReason.AGENT_ASKED, ["Should add() take floats?"])
    assert got.question == "Should add() take floats?"
