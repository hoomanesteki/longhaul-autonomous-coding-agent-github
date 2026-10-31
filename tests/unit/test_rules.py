"""The turn rules that the in-process loop and the durable workflow share.

These are decisions with no I/O: what a stop reason leads to, when a turn is sampled again,
when a run ends, and which calls run. loop.py and TaskWorkflow both call them, so a rule
cannot be right in one and wrong in the other. tests/unit/test_loop.py pins the same rules
through the whole loop, and tests/integration/test_parity.py runs the loop scripts through the
workflow and compares the transcripts.
"""

import ast
from pathlib import Path
from typing import Any, get_args

import pytest

from longhaul.agent import rules
from longhaul.agent.turns import (
    CONTROL_RESULT,
    answer_tool_calls,
    validate_pairs,
)
from longhaul.contracts import (
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    ProviderErrorKind,
    StopDetails,
    StopReason,
    TextBlock,
    ToolCall,
    ToolResult,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from longhaul.tools.control import FINISH

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
ALL_STOPS: tuple[StopReason, ...] = get_args(StopReason)


def reply(
    stop_reason: StopReason,
    *,
    details: StopDetails | None = None,
    raw: str | None = None,
    content: list[Any] | None = None,
    calls: list[ToolCall] | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content or [],
        tool_calls=calls or [],
        stop_reason=stop_reason,
        raw_stop_reason=raw,
        stop_details=details,
        usage=Usage(),
        model="m",
        latency_ms=0,
    )


def error(summary: str) -> Outcome:
    return Outcome(kind=OutcomeKind.ERROR, summary=summary)


# ---------- the summaries ----------
def test_the_fixed_summaries_are_the_ones_callers_read() -> None:
    # A caller tells the ends apart by these strings, so they are part of the interface.
    assert rules.TURN_LIMIT == "turn limit"
    assert rules.OUTPUT_LIMIT == "output limit"
    assert rules.CONTEXT_LIMIT == "context limit"
    assert rules.PAUSE_LIMIT == "pause limit"
    assert rules.NO_TOOL_CALLS == "no tool calls"
    assert rules.NUDGE == "Use tools or call finish."
    assert rules.MAX_PAUSES == 5


def test_finish_is_the_name_of_the_finish_tool() -> None:
    # The rules cannot import the tools package, which the workflow must not load.
    assert rules.FINISH == FINISH


def first_party_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return {name for name in found if name.split(".")[0] == "longhaul"}


def test_the_rules_import_nothing_but_the_contracts() -> None:
    # The workflow imports this module, and everything it imports runs in the Temporal
    # sandbox. Tools, turns and providers reach the Docker SDK, jsonschema and the model SDKs.
    assert first_party_imports(ROOT / "src/longhaul/agent/rules.py") == {"longhaul.contracts"}


# ---------- one reply: judge ----------
@pytest.mark.parametrize("stop", ["end_turn", "tool_use"])
def test_a_finished_reply_is_used(stop: StopReason) -> None:
    verdict = rules.judge(rules.Sampling(), reply(stop))
    assert verdict == rules.Judgement(rules.Move.USE, rules.Sampling())


def test_only_end_turn_and_tool_use_are_complete() -> None:
    complete = {s for s in ALL_STOPS if rules.is_complete(reply(s))}
    assert complete == {"end_turn", "tool_use"}


@pytest.mark.parametrize("stop", ALL_STOPS)
def test_is_complete_agrees_with_judge_for_every_stop_reason(stop: StopReason) -> None:
    # A reply is stored exactly when judge says to use it, whatever the sampling state is.
    for sampling in (rules.Sampling(), rules.Sampling(retried=True, pauses=rules.MAX_PAUSES)):
        used = rules.judge(sampling, reply(stop)).move is rules.Move.USE
        assert used == rules.is_complete(reply(stop))


def test_max_tokens_is_retried_once_then_ends_the_run() -> None:
    first = rules.judge(rules.Sampling(), reply("max_tokens"))
    assert first == rules.Judgement(rules.Move.RETRY, rules.Sampling(retried=True))

    second = rules.judge(first.sampling, reply("max_tokens"))
    assert second.move is rules.Move.END
    assert second.outcome == error("output limit")
    # The state stays as it was, so a caller that logs it sees what the run had used.
    assert second.sampling == rules.Sampling(retried=True)


def test_pause_turn_continues_until_the_pause_limit() -> None:
    sampling = rules.Sampling()
    for count in range(1, rules.MAX_PAUSES + 1):
        verdict = rules.judge(sampling, reply("pause_turn"))
        assert verdict.move is rules.Move.CONTINUE
        assert verdict.sampling.pauses == count
        sampling = verdict.sampling

    over = rules.judge(sampling, reply("pause_turn"))
    assert over.move is rules.Move.END
    assert over.outcome == error("pause limit")


def test_a_pause_does_not_use_up_the_max_tokens_retry() -> None:
    paused = rules.judge(rules.Sampling(), reply("pause_turn")).sampling
    assert paused.retried is False
    assert rules.judge(paused, reply("max_tokens")).move is rules.Move.RETRY


@pytest.mark.parametrize(
    ("stop", "details", "raw", "summary"),
    [
        (
            "refusal",
            StopDetails(category="cyber", explanation="The request looks like malware."),
            None,
            "model declined (cyber): The request looks like malware.",
        ),
        (
            "refusal",
            StopDetails(category="bio"),
            None,
            "model declined (bio): no explanation given",
        ),
        ("refusal", StopDetails(), None, "model declined (unknown): no explanation given"),
        ("refusal", None, None, "model declined (unknown): no explanation given"),
        (
            "refusal",
            StopDetails(category="  cyber ", explanation="  why  "),
            None,
            "model declined (cyber): why",
        ),
        ("model_context_window_exceeded", None, None, "context limit"),
        ("stop_sequence", None, None, "unexpected stop reason: stop_sequence"),
        ("other", None, "weird_reason", "unexpected stop reason: weird_reason"),
        ("other", None, None, "unexpected stop reason: other"),
    ],
)
def test_the_stop_reasons_that_end_the_run(
    stop: StopReason, details: StopDetails | None, raw: str | None, summary: str
) -> None:
    verdict = rules.judge(rules.Sampling(), reply(stop, details=details, raw=raw))
    assert verdict.move is rules.Move.END
    assert verdict.outcome == error(summary)


def test_judge_reads_only_the_stop_fields_not_the_content() -> None:
    # D9: stop_reason first. A refusal with a tool call in it is still a refusal.
    stray = ToolUseBlock(id="t1", name="write_file", input={"path": "x"})
    verdict = rules.judge(
        rules.Sampling(),
        reply("refusal", content=[stray], calls=[ToolCall(id="t1", name="write_file", input={})]),
    )
    assert verdict.move is rules.Move.END


# ---------- which calls run ----------
def test_calls_run_only_after_a_tool_use_stop() -> None:
    calls = [
        ToolCall(id="a", name="read_file", input={}),
        ToolCall(id="b", name="finish", input={}),
    ]
    assert rules.runnable_calls(reply("tool_use"), calls) == calls
    for stop in ALL_STOPS:
        if stop != "tool_use":
            assert rules.runnable_calls(reply(stop), calls) == []


def test_runnable_calls_returns_a_new_list() -> None:
    calls = [ToolCall(id="a", name="read_file", input={})]
    assert rules.runnable_calls(reply("tool_use"), calls) is not calls


# ---------- idle turns ----------
def test_the_first_turn_without_a_tool_call_is_nudged_and_the_second_ends_the_run() -> None:
    first = rules.idle_turn(0)
    assert first == rules.Idle(count=1, outcome=None)

    second = rules.idle_turn(first.count)
    assert second.count == 2
    assert second.outcome == error("no tool calls")


# ---------- finish and the calls around it ----------
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"summary": "fixed it"}, "fixed it"),
        ({"summary": "  fixed it  \n"}, "fixed it"),
        ({"summary": ""}, None),
        ({"summary": "   "}, None),
        ({"summary": 3}, None),
        ({"summary": None}, None),
        ({}, None),
        (None, None),
    ],
)
def test_finish_summary(arguments: dict[str, Any] | None, expected: str | None) -> None:
    assert rules.finish_summary(arguments) == expected


def test_finished_is_the_one_good_end() -> None:
    assert rules.finished("done") == Outcome(kind=OutcomeKind.PR_OPENED, summary="done")


def test_the_turn_limit_and_provider_errors_have_their_outcomes() -> None:
    assert rules.turn_limit_outcome() == error("turn limit")
    assert rules.provider_error_outcome(ProviderErrorKind.CONTEXT_TOO_LONG) == error(
        "context limit"
    )
    # Every other kind belongs to the caller's retry policy.
    others = [k for k in ProviderErrorKind if k is not ProviderErrorKind.CONTEXT_TOO_LONG]
    assert [rules.provider_error_outcome(kind) for kind in others] == [None] * len(others)


class Call:
    def __init__(self, call_id: str, name: str, **arguments: Any) -> None:
        self.id, self.name, self.input = call_id, name, arguments or None


async def run_all(calls: list[Call], ran: list[str]) -> rules.CallRun:
    async def run(call: Call) -> ToolResult:
        ran.append(call.id)
        return ToolResult(call_id=call.id, ok=True, output=f"ran {call.name}")

    return await rules.run_calls(calls, run)


async def test_calls_run_one_after_another_in_order() -> None:
    ran: list[str] = []
    outcome = await run_all([Call("a", "read_file"), Call("b", "run_tests")], ran)
    assert ran == ["a", "b"]
    assert [r.call_id for r in outcome.results] == ["a", "b"]
    assert outcome.finish is None


async def test_a_good_finish_stops_the_calls_and_gives_its_summary() -> None:
    ran: list[str] = []
    calls = [
        Call("a", "read_file"),
        Call("f", "finish", summary="all done"),
        Call("c", "edit_file"),
    ]
    outcome = await run_all(calls, ran)
    # Nothing runs for finish, and nothing runs after it.
    assert ran == ["a"]
    assert [r.call_id for r in outcome.results] == ["a"]
    assert outcome.finish == "all done"


@pytest.mark.parametrize("arguments", [{}, {"summary": ""}, {"summary": "  "}, {"summary": 5}])
async def test_a_bad_finish_is_answered_with_an_error_and_the_calls_go_on(
    arguments: dict[str, Any],
) -> None:
    ran: list[str] = []
    outcome = await run_all([Call("f", "finish", **arguments), Call("b", "read_file")], ran)
    assert ran == ["b"]
    bad, later = outcome.results
    assert (bad.call_id, bad.ok, bad.output) == ("f", False, rules.BAD_FINISH)
    assert later.call_id == "b"
    assert outcome.finish is None


async def test_a_finish_whose_arguments_are_by_reference_counts_as_bad() -> None:
    # The workflow sees `input` as None when the arguments are over 16 KB. A summary that long
    # is not the short text the prompt asks for.
    outcome = await run_all([Call("f", "finish")], [])
    assert outcome.finish is None
    assert outcome.results[0].output == rules.BAD_FINISH


async def test_an_error_from_a_call_is_not_caught() -> None:
    async def run(call: Call) -> ToolResult:
        raise RuntimeError("the sandbox is gone")

    with pytest.raises(RuntimeError, match="sandbox is gone"):
        await rules.run_calls([Call("a", "read_file")], run)


# ---------- text carried over a pause ----------
def test_only_text_carries_over_a_pause() -> None:
    resp = reply(
        "pause_turn",
        content=[
            TextBlock(text="Checking the repo first."),
            TextBlock(text="   "),
            ToolUseBlock(id="t1", name="finish", input={"summary": "x"}),
        ],
    )
    assert rules.carried_text(resp) == [TextBlock(text="Checking the repo first.")]


def test_the_stored_reply_starts_with_the_text_from_before_the_pauses() -> None:
    paused = [TextBlock(text="before")]
    final = reply("end_turn", content=[TextBlock(text="after")])
    joined = rules.join_pauses(final, paused)
    assert joined.content == [TextBlock(text="before"), TextBlock(text="after")]
    assert final.content == [TextBlock(text="after")]  # the reply itself is not changed


def test_a_reply_after_no_pause_is_returned_as_it_is() -> None:
    final = reply("end_turn", content=[TextBlock(text="after")])
    assert rules.join_pauses(final, []) is final


# ---------- answering the calls: the pairing rule of D9 ----------
def test_the_calls_a_good_finish_skipped_are_answered_with_the_reason() -> None:
    transcript = [
        Message(role="user", content=[TextBlock(text="go")]),
        Message(
            role="assistant",
            content=[
                ToolUseBlock(id="a", name="read_file", input={}),
                ToolUseBlock(id="f", name="finish", input={"summary": "done"}),
                ToolUseBlock(id="c", name="edit_file", input={}),
            ],
        ),
    ]
    calls = [
        ToolCall(id="a", name="read_file", input={}),
        ToolCall(id="f", name="finish", input={"summary": "done"}),
        ToolCall(id="c", name="edit_file", input={}),
    ]
    answered = answer_tool_calls(
        transcript, calls, [ToolResult(call_id="a", ok=True, output="contents")]
    )
    ran, ended, skipped = [b for b in answered[-1].content if isinstance(b, ToolResultBlock)]
    assert (ran.content, ran.is_error) == ("contents", False)
    assert (ended.content, ended.is_error) == (CONTROL_RESULT, False)
    assert skipped.is_error
    assert skipped.content == f"not run: {rules.SKIPPED_AFTER_FINISH}"
    validate_pairs(answered)
