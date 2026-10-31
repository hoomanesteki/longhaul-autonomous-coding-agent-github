"""Turn functions: the model call, the tool call, and the edits they make to a transcript.

The edits keep one rule the Messages API enforces with a 400 that is never retried: every
tool_use gets exactly one tool_result in the next user message, in call order, and any
harness text goes after the results (D9).
"""

import copy
from collections.abc import AsyncIterator
from typing import Any

import pytest

from longhaul.agent.turns import (
    CONTROL_RESULT,
    DEFAULT_NOT_RUN_REASON,
    TranscriptError,
    append_assistant,
    append_tool_results,
    append_user_text,
    llm_step,
    tool_step,
    validate_pairs,
)
from longhaul.contracts import (
    CallOptions,
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    Sandbox,
    StopReason,
    TextBlock,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import ToolRegistry, default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")


# ---------- builders ----------
def text(body: str) -> TextBlock:
    return TextBlock(text=body)


def use(call_id: str, name: str = "run_command", **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name=name, input=args)


def res(call_id: str, content: str = "ok", is_error: bool = False) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=call_id, content=content, is_error=is_error)


def user(*blocks: Any) -> Message:
    return Message(role="user", content=list(blocks))


def assistant(*blocks: Any) -> Message:
    return Message(role="assistant", content=list(blocks))


def call(call_id: str, name: str = "run_command", **args: Any) -> ToolCall:
    return ToolCall(id=call_id, name=name, input=args)


def result(call_id: str, output: str = "fine", ok: bool = True) -> ToolResult:
    return ToolResult(call_id=call_id, ok=ok, output=output)


def reply(
    *blocks: Any, stop_reason: StopReason = "end_turn", calls: list[ToolCall] | None = None
) -> LLMResponse:
    return LLMResponse(
        content=list(blocks),
        tool_calls=calls or [],
        stop_reason=stop_reason,
        usage=Usage(),
        model="m",
        latency_ms=0,
    )


def two_calls() -> tuple[list[Message], list[ToolCall]]:
    """A transcript that ends on an assistant turn with two parallel calls, and those calls."""
    transcript = [
        user(text("go")),
        assistant(use("t1", "read_file", path="a"), use("t2", "list_dir")),
    ]
    return transcript, [call("t1", "read_file", path="a"), call("t2", "list_dir")]


# ---------- llm_step ----------
class RecordingProvider:
    """Keeps what it was asked and answers with one canned response or error."""

    name = "recording"

    def __init__(self, response: LLMResponse | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.requests: list[dict[str, Any]] = []

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        cache_system: bool = True,
        options: CallOptions | None = None,
    ) -> LLMResponse:
        self.requests.append(
            {
                "model": model,
                "system": system,
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "cache_system": cache_system,
                "options": options,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

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


async def test_llm_step_sends_the_request_and_returns_the_reply_untouched() -> None:
    answer = reply(text("hi"))
    provider = RecordingProvider(answer)
    transcript = [user(text("go"))]
    tools = [ToolSpec(name="x", description="d", input_schema={"type": "object"})]
    got = await llm_step(
        provider, "model-1", "Role: coder", transcript, tools, 321, CallOptions(effort="low")
    )
    assert got is answer
    assert provider.requests == [
        {
            "model": "model-1",
            "system": "Role: coder",
            "messages": transcript,
            "tools": tools,
            "max_tokens": 321,
            "cache_system": True,
            "options": CallOptions(effort="low"),
        }
    ]


async def test_llm_step_options_are_optional() -> None:
    provider = RecordingProvider(reply(text("hi")))
    await llm_step(provider, "m", "Role: coder", [user(text("go"))], [], 10)
    assert provider.requests[0]["options"] is None


async def test_llm_step_lets_a_provider_error_through_for_the_retry_policy() -> None:
    # Phase 2 wraps this in an activity, and Temporal decides what to retry from the error.
    error = ProviderError(ProviderErrorKind.OVERLOADED, "busy", retry_after_s=3.0)
    with pytest.raises(ProviderError) as caught:
        await llm_step(RecordingProvider(error=error), "m", "s", [user(text("go"))], [], 10)
    assert caught.value is error


# ---------- tool_step ----------
class BoomTool:
    spec = ToolSpec(name="boom", description="Always breaks.", input_schema={"type": "object"})
    mutates = False

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise RuntimeError("kaput")


@pytest.fixture
async def sbx() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox()
    yield box
    await box.destroy()


async def test_tool_step_runs_the_call_through_the_registry(sbx: FakeSandbox) -> None:
    await sbx.write_file("notes.txt", b"hello\n")
    r = await tool_step(default_registry(), sbx, call("c9", "read_file", path="notes.txt"), CTX)
    assert r.ok and r.call_id == "c9"
    assert "hello" in r.output


async def test_tool_step_turns_bad_input_into_a_result(sbx: FakeSandbox) -> None:
    registry = default_registry()
    missing = await tool_step(registry, sbx, call("c1", "read_file"), CTX)
    assert not missing.ok and missing.call_id == "c1" and "path" in missing.output
    unknown = await tool_step(registry, sbx, call("c2", "no_such_tool"), CTX)
    assert not unknown.ok and "unknown tool" in unknown.output


async def test_tool_step_does_not_hide_errors_that_are_not_the_models(sbx: FakeSandbox) -> None:
    registry = ToolRegistry()
    registry.register(BoomTool())
    with pytest.raises(RuntimeError, match="kaput"):
        await tool_step(registry, sbx, call("c", "boom"), CTX)


# ---------- append_assistant ----------
def test_append_assistant_stores_text_and_calls_in_order() -> None:
    transcript = [user(text("go"))]
    answer = reply(
        text("Looking."),
        use("t1", path="a"),
        stop_reason="tool_use",
        calls=[call("t1", path="a")],
    )
    out = append_assistant(transcript, answer)
    assert out == [*transcript, assistant(text("Looking."), use("t1", path="a"))]


@pytest.mark.parametrize(
    "blocks",
    [
        [],
        [TextBlock(text="")],
        [TextBlock(text=" \n\t ")],
        [TextBlock(text=""), TextBlock(text="  ")],
    ],
    ids=["no blocks", "empty text", "blank text", "several blank texts"],
)
def test_an_empty_reply_is_never_stored(blocks: list[TextBlock]) -> None:
    # The API answers 400 to an assistant message with nothing in it, and render.py would
    # skip it and leave two user messages side by side.
    transcript = [user(text("go"))]
    out = append_assistant(transcript, reply(*blocks))
    assert out == transcript
    assert out is not transcript


@pytest.mark.parametrize(
    "stop_reason",
    [
        "end_turn",
        "max_tokens",
        "pause_turn",
        "refusal",
        "stop_sequence",
        "model_context_window_exceeded",
        "other",
    ],
)
def test_a_tool_use_is_stored_only_after_a_tool_use_stop(stop_reason: StopReason) -> None:
    # Only a tool_use stop gets its calls run (D9). A tool_use block from any other reply
    # would never be answered, and an unanswered tool_use is a 400.
    answer = reply(text("Hmm."), use("t1"), stop_reason=stop_reason, calls=[call("t1")])
    out = append_assistant([user(text("go"))], answer)
    assert out[-1] == assistant(text("Hmm."))
    validate_pairs(out)


def test_a_reply_that_is_only_a_cut_off_call_is_not_stored() -> None:
    transcript = [user(text("go"))]
    cut = reply(use("t1", content="x = "), stop_reason="max_tokens", calls=[call("t1")])
    assert append_assistant(transcript, cut) == transcript


def test_blank_text_next_to_a_call_is_dropped() -> None:
    answer = reply(text("  "), use("t1"), stop_reason="tool_use", calls=[call("t1")])
    out = append_assistant([user(text("go"))], answer)
    assert out[-1] == assistant(use("t1"))


def test_append_assistant_returns_a_new_list_and_leaves_the_old_one() -> None:
    transcript = [user(text("go"))]
    before = copy.deepcopy(transcript)
    out = append_assistant(transcript, reply(text("hi")))
    assert transcript == before
    assert out is not transcript and len(out) == 2


def test_an_assistant_message_needs_a_user_message_before_it() -> None:
    with pytest.raises(TranscriptError, match="user message"):
        append_assistant([], reply(text("hi")))
    with pytest.raises(TranscriptError, match="user message"):
        append_assistant([user(text("go")), assistant(text("a"))], reply(text("b")))


# ---------- append_tool_results ----------
def test_results_are_written_in_call_order() -> None:
    transcript, calls = two_calls()
    # Handed over in the opposite order on purpose.
    out = append_tool_results(transcript, calls, [result("t2", "tree"), result("t1", "file")])
    assert out[:-1] == transcript
    assert out[-1] == user(res("t1", "file"), res("t2", "tree"))
    validate_pairs(out)


def test_a_failed_result_becomes_an_error_result() -> None:
    transcript, calls = two_calls()
    out = append_tool_results(
        transcript, calls, [result("t1", "no such file", ok=False), result("t2")]
    )
    assert out[-1] == user(res("t1", "no such file", is_error=True), res("t2", "fine"))


def test_a_control_tool_gets_a_short_ok_result() -> None:
    # finish is handled by the loop, so nothing ran for it, and it still needs its result.
    transcript = [user(text("go")), assistant(use("t1", "finish", summary="done"))]
    out = append_tool_results(transcript, [call("t1", "finish", summary="done")], [])
    assert out[-1] == user(res("t1", CONTROL_RESULT))
    assert len(CONTROL_RESULT) < 40
    validate_pairs(out)


def test_a_result_that_was_given_for_a_control_tool_is_kept() -> None:
    transcript = [user(text("go")), assistant(use("t1", "finish"))]
    out = append_tool_results(
        transcript, [call("t1", "finish")], [result("t1", "finish needs a summary", ok=False)]
    )
    assert out[-1] == user(res("t1", "finish needs a summary", is_error=True))


def test_a_call_that_did_not_run_gets_a_not_run_error() -> None:
    transcript, calls = two_calls()
    out = append_tool_results(
        transcript,
        calls,
        [result("t1", "file")],
        not_run_reason="finish was called earlier in this turn",
    )
    assert out[-1] == user(
        res("t1", "file"),
        res("t2", "not run: finish was called earlier in this turn", is_error=True),
    )
    validate_pairs(out)


def test_the_default_reason_still_says_not_run() -> None:
    transcript, calls = two_calls()
    out = append_tool_results(transcript, calls, [])
    assert out[-1] == user(
        res("t1", f"not run: {DEFAULT_NOT_RUN_REASON}", is_error=True),
        res("t2", f"not run: {DEFAULT_NOT_RUN_REASON}", is_error=True),
    )


def test_a_result_for_a_call_outside_the_turn_is_refused() -> None:
    transcript, calls = two_calls()
    with pytest.raises(TranscriptError, match="t9"):
        append_tool_results(transcript, calls, [result("t9")])


def test_two_results_for_one_call_are_refused() -> None:
    transcript, calls = two_calls()
    with pytest.raises(TranscriptError, match="t1"):
        append_tool_results(transcript, calls, [result("t1"), result("t1")])


def test_the_calls_must_be_the_ones_in_the_last_assistant_message() -> None:
    transcript, calls = two_calls()
    with pytest.raises(TranscriptError, match="t1"):
        append_tool_results(transcript, calls[:1], [result("t1")])  # t2 would stay unanswered
    with pytest.raises(TranscriptError):
        append_tool_results(transcript, list(reversed(calls)), [])  # call order is the order
    with pytest.raises(TranscriptError):
        append_tool_results([user(text("go"))], calls, [])  # nothing made these calls
    with pytest.raises(TranscriptError):
        append_tool_results([], calls, [])


def test_results_are_not_added_twice() -> None:
    transcript, calls = two_calls()
    done = append_tool_results(transcript, calls, [result("t1"), result("t2")])
    with pytest.raises(TranscriptError):
        append_tool_results(done, calls, [result("t1"), result("t2")])


def test_no_calls_means_no_message() -> None:
    transcript = [user(text("go")), assistant(text("done"))]
    out = append_tool_results(transcript, [], [])
    assert out == transcript
    assert out is not transcript


def test_append_tool_results_leaves_its_input_alone() -> None:
    transcript, calls = two_calls()
    before = copy.deepcopy(transcript)
    out = append_tool_results(transcript, calls, [result("t1"), result("t2")])
    assert transcript == before
    assert out is not transcript and len(out) == len(transcript) + 1


# ---------- append_user_text ----------
def test_text_goes_after_the_tool_results_of_the_last_user_message() -> None:
    transcript = [user(text("go")), assistant(use("t1")), user(res("t1"))]
    before = copy.deepcopy(transcript)
    out = append_user_text(transcript, "Use tools or call finish.")
    assert len(out) == len(transcript)
    assert out[-1] == user(res("t1"), text("Use tools or call finish."))
    assert transcript == before
    validate_pairs(out)


def test_text_after_an_assistant_message_without_tool_use_is_a_new_user_message() -> None:
    transcript = [user(text("go")), assistant(text("thinking"))]
    out = append_user_text(transcript, "Use tools or call finish.")
    assert out == [*transcript, user(text("Use tools or call finish."))]
    assert out is not transcript and len(transcript) == 2


def test_text_joins_a_user_message_that_is_last() -> None:
    # An empty reply is not stored, so the transcript can still end on the first message.
    assert append_user_text([user(text("go"))], "more") == [user(text("go"), text("more"))]


def test_text_can_start_a_transcript() -> None:
    assert append_user_text([], "go") == [user(text("go"))]


def test_text_cannot_come_before_the_tool_results() -> None:
    transcript = [user(text("go")), assistant(use("t1"))]
    with pytest.raises(TranscriptError, match="tool"):
        append_user_text(transcript, "Use tools or call finish.")


@pytest.mark.parametrize("blank", ["", "  \n\t"])
def test_blank_text_is_refused(blank: str) -> None:
    # A blank text block is a 400, and render.py would drop it and leave an empty message.
    with pytest.raises(ValueError, match="blank"):
        append_user_text([user(text("go"))], blank)


# ---------- validate_pairs ----------
def paired() -> list[Message]:
    return [
        user(text("go")),
        assistant(text("on it"), use("t1"), use("t2")),
        user(res("t1"), res("t2", is_error=True), text("harness note")),
        assistant(use("t3")),
        user(res("t3")),
        assistant(text("done")),
    ]


def test_a_paired_transcript_passes() -> None:
    validate_pairs(paired())
    validate_pairs([])
    validate_pairs([user(text("go"))])


def test_transcript_error_is_a_value_error() -> None:
    assert issubclass(TranscriptError, ValueError)


@pytest.mark.parametrize(
    ("broken", "failure", "names"),
    [
        ([user(text("go")), assistant(use("t1"))], "no tool_result after it", "t1"),
        ([user(text("go")), assistant(use("t1")), user(text("hi"))], "no tool_result for", "t1"),
        ([user(text("go")), assistant(text("hi")), user(res("t9"))], "with no tool_use", "t9"),
        ([user(res("t9"))], "with no tool_use", "t9"),
        ([user(text("go")), assistant(use("t1")), user(res("t2"))], "with no tool_use", "t2"),
        (
            [user(text("go")), assistant(use("t1"), use("t2")), user(res("t1"))],
            "no tool_result for",
            "t2",
        ),
        (
            [user(text("go")), assistant(use("t1"), use("t2")), user(res("t2"), res("t1"))],
            "out of call order",
            "t1",
        ),
        (
            [user(text("go")), assistant(use("t1")), user(text("note"), res("t1"))],
            "text before a tool_result",
            "",
        ),
        (
            [
                user(text("go")),
                assistant(use("t1"), use("t2")),
                user(res("t1"), text("n"), res("t2")),
            ],
            "text before a tool_result",
            "",
        ),
        (
            [user(text("go")), assistant(use("t1")), user(res("t1"), res("t1"))],
            "more than one tool_result",
            "t1",
        ),
        (
            [user(text("go")), assistant(use("t1")), assistant(text("again"))],
            "another assistant message",
            "t1",
        ),
        (
            [user(text("go")), assistant(use("t1")), user(res("t1")), assistant(use("t1"))],
            "reuses tool_use ids",
            "t1",
        ),
        ([user(use("t1"))], "user message with a tool_use", ""),
        ([user(text("go")), assistant(res("t1"))], "assistant message with a tool_result", ""),
    ],
    ids=[
        "tool_use never answered",
        "answered by text only",
        "result with no tool_use",
        "result as the first message",
        "result for another id",
        "one of two results missing",
        "results out of call order",
        "text before the result",
        "text between results",
        "result twice",
        "assistant follows open tool_use",
        "tool_use id reused",
        "tool_use in a user message",
        "tool_result in an assistant message",
    ],
)
def test_an_unpaired_transcript_is_refused(broken: list[Message], failure: str, names: str) -> None:
    with pytest.raises(TranscriptError, match=failure) as caught:
        validate_pairs(broken)
    assert names in str(caught.value)


def test_the_error_says_which_message_is_wrong() -> None:
    transcript = [*paired()[:-1], assistant(use("t4"))]
    with pytest.raises(TranscriptError, match="message 5"):
        validate_pairs(transcript)
