"""The llm_turn activity: one model call, its blobs, its summaries and its row.

The activity runs inside temporalio's ActivityEnvironment with a fake provider, the memory
blob store and the memory recorder. tests/integration/test_activities.py runs it for real
inside a workflow, and tests/integration/test_parity.py compares it with the in-process loop.
"""

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import FIRST_USER_MESSAGE, build_system_prompt
from longhaul.blobs.base import blob_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    BlobRef,
    CallOptions,
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    RepoSettings,
    Role,
    StopDetails,
    StopReason,
    TaskSpec,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.payload_guard import DEFAULT_LIMIT, json_size
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

CFG = LoopConfig(
    model="test-model",
    max_turns=10,
    max_tokens_per_turn=1000,
    max_tokens_retry=5000,
    effort="medium",
)
SPEC = TaskSpec(
    task_id="lh-acme-calc-42",
    repo="acme/calc",
    issue_number=42,
    title="add() subtracts",
    body="add(2, 3) returns -1",
    settings=RepoSettings(repo="acme/calc"),
)
TOOLS = default_registry().specs()
FIRST = Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])
USAGE = Usage(input_tokens=100, output_tokens=20, cache_read_tokens=5, cache_write_tokens=7)

Step = LLMResponse | ProviderError | Callable[[], Awaitable[LLMResponse]]


class FakeProvider:
    """Answers with the steps it was given, in order, and keeps what each call was given."""

    name = "fake"

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        assert self.steps, "the activity asked for more replies than the test gave"
        step = self.steps.pop(0)
        if isinstance(step, ProviderError):
            raise step
        if isinstance(step, LLMResponse):
            return step
        return await step()

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


def reply(stop: StopReason, *blocks: Any, **fields: Any) -> LLMResponse:
    calls = [
        ToolCall(id=b.id, name=b.name, input=b.input) for b in blocks if isinstance(b, ToolUseBlock)
    ]
    return LLMResponse(
        content=list(blocks),
        tool_calls=calls,
        stop_reason=stop,
        usage=fields.pop("usage", USAGE),
        model="test-model",
        latency_ms=fields.pop("latency_ms", 42),
        **fields,
    )


def use(call_id: str, name: str = "read_file", **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name=name, input=args)


class Rig:
    """The activity with its fakes, and a way to run it the way a worker would."""

    def __init__(self, *steps: Step, config: Callable[[Role], LoopConfig] | None = None) -> None:
        self.provider = FakeProvider(*steps)
        self.blobs = MemoryBlobStore()
        self.recorder = MemoryRecorder()
        self.activities = LlmActivities(
            self.provider, self.blobs, self.recorder, TOOLS, config or (lambda role: CFG)
        )
        self.beats: list[Any] = []

    async def run(
        self,
        inp: acts.LlmTurnInput,
        *,
        attempt: int = 1,
        heartbeat_timeout_s: float | None = None,
    ) -> acts.LlmTurnOutput:
        env = ActivityEnvironment()
        timeout = None if heartbeat_timeout_s is None else timedelta(seconds=heartbeat_timeout_s)
        env.info = dataclasses.replace(env.info, attempt=attempt, heartbeat_timeout=timeout)
        env.on_heartbeat = lambda *details: self.beats.append(details)
        return await env.run(self.activities.llm_turn, inp)

    async def put(self, key_name: str, model: Any, turn: int = 0) -> BlobRef:
        return await self.blobs.put_json(model, blob_key(SPEC.task_id, "turns", turn, key_name))

    async def transcript(self, ref: BlobRef) -> list[Message]:
        return await self.blobs.get_json(ref, list[Message])


def turn_input(**fields: Any) -> acts.LlmTurnInput:
    values: dict[str, Any] = {
        "task_id": SPEC.task_id,
        "turn_index": 0,
        "role": Role.CODER,
        "spec": SPEC,
        "transcript_ref": None,
    }
    return acts.LlmTurnInput(**{**values, **fields})


def answered(call_id: str = "toolu_1") -> list[Message]:
    """A transcript that has been through one tool turn, ready for the second."""
    return [
        FIRST,
        Message(role="assistant", content=[use(call_id, path="a.py")]),
        Message(role="user", content=[ToolResultBlock(tool_use_id=call_id, content="x = 1")]),
    ]


# ---------- the first turn ----------
async def test_the_first_turn_builds_the_request_from_the_spec() -> None:
    rig = Rig(reply("tool_use", use("toolu_1", path="a.py")))
    await rig.run(turn_input())

    (call,) = rig.provider.calls
    assert call["system"] == build_system_prompt(Role.CODER, SPEC)
    assert call["model"] == "test-model"
    assert call["max_tokens"] == CFG.max_tokens_per_turn
    assert call["options"] == CallOptions(effort="medium")
    assert call["messages"] == [FIRST]
    assert call["tools"] == TOOLS


async def test_no_effort_in_the_config_sends_no_options() -> None:
    config = CFG.model_copy(update={"effort": None})
    rig = Rig(reply("end_turn"), config=lambda role: config)
    await rig.run(turn_input())
    assert rig.provider.calls[0]["options"] is None


async def test_the_reply_is_stored_in_the_transcript_and_summarized() -> None:
    rig = Rig(reply("tool_use", TextBlock(text="Looking."), use("toolu_1", path="a.py")))
    out = await rig.run(turn_input())

    assert out.stop_reason == "tool_use"
    assert out.usage == USAGE
    assert out.input_tokens_total == 112  # input + cache read + cache write
    assert out.cost_usd == 0.0
    assert out.tool_call_summaries == [
        acts.ToolCallSummary(id="toolu_1", name="read_file", input={"path": "a.py"})
    ]
    assert await rig.transcript(out.transcript_ref) == [
        FIRST,
        Message(
            role="assistant", content=[TextBlock(text="Looking."), use("toolu_1", path="a.py")]
        ),
    ]


async def test_the_response_and_every_call_are_stored_in_full() -> None:
    rig = Rig(reply("tool_use", use("toolu_1", path="a.py"), use("toolu_2", path="b.py")))
    out = await rig.run(turn_input())

    assert out.response_ref is not None
    response = await rig.blobs.get_json(out.response_ref, LLMResponse)
    assert response.stop_reason == "tool_use"
    assert out.tool_calls_ref is not None
    calls = await rig.blobs.get_json(out.tool_calls_ref, list[ToolCall])
    assert [c.id for c in calls] == ["toolu_1", "toolu_2"]


async def test_the_blobs_of_a_turn_live_under_its_folder() -> None:
    rig = Rig(reply("tool_use", use("toolu_1", path="a.py")))
    out = await rig.run(
        turn_input(turn_index=7, transcript_ref=await rig.put("t.json", answered()))
    )

    prefix = "tasks/lh-acme-calc-42/turns/0007/"
    assert out.response_ref is not None and out.tool_calls_ref is not None
    assert out.response_ref.key == prefix + "response-1.json"
    assert out.transcript_ref.key == prefix + "transcript-1.json"
    assert out.tool_calls_ref.key == prefix + "tool_calls-1.json"


async def test_a_turn_row_is_written_for_the_call() -> None:
    rig = Rig(reply("tool_use", use("toolu_1", path="a.py")))
    out = await rig.run(turn_input())

    (row,) = rig.recorder.turns
    assert (row.task_id, row.turn_index, row.attempt, row.role) == (SPEC.task_id, 0, 1, "coder")
    assert row.response_ref is not None and out.response_ref is not None
    assert row.response_ref == out.response_ref.key
    assert row.request_ref is None  # the first transcript was made here, not read from a blob
    assert (row.usage, row.cost_usd) == (USAGE, 0.0)
    assert (row.stop_reason, row.latency_ms) == ("tool_use", 42)
    assert row.ended_at is not None and row.ended_at >= row.started_at


async def test_a_later_turn_continues_from_the_stored_transcript() -> None:
    rig = Rig(reply("end_turn", TextBlock(text="Done.")))
    ref = await rig.put("transcript-1.json", answered())
    out = await rig.run(turn_input(turn_index=1, transcript_ref=ref))

    assert rig.provider.calls[0]["messages"] == answered()
    stored = await rig.transcript(out.transcript_ref)
    assert stored[:-1] == answered()
    assert stored[-1] == Message(role="assistant", content=[TextBlock(text="Done.")])
    assert rig.recorder.turns[0].request_ref == ref.key


# ---------- D8: the output cap ----------
async def test_the_retry_of_a_cut_turn_runs_at_the_higher_cap() -> None:
    rig = Rig(reply("end_turn", TextBlock(text="Short.")))
    await rig.run(turn_input(high_output_cap=True))
    assert rig.provider.calls[0]["max_tokens"] == CFG.max_tokens_retry


async def test_a_reply_cut_off_at_max_tokens_is_not_stored_and_none_of_its_calls_run() -> None:
    cut = reply("max_tokens", TextBlock(text="Writing it all"), use("toolu_1", path="partial"))
    rig = Rig(cut)
    ref = await rig.put("transcript-1.json", answered())
    out = await rig.run(turn_input(turn_index=1, transcript_ref=ref))

    assert out.stop_reason == "max_tokens"
    assert out.transcript_ref == ref  # nothing was added
    assert out.tool_call_summaries == []
    assert out.tool_calls_ref is None
    # The call was billed, so it still has its row and its response.
    (row,) = rig.recorder.turns
    assert row.stop_reason == "max_tokens"
    assert row.response_ref is not None


async def test_a_cut_first_turn_still_gives_the_workflow_a_transcript_to_carry() -> None:
    rig = Rig(reply("max_tokens", TextBlock(text="Writing")))
    out = await rig.run(turn_input())
    assert await rig.transcript(out.transcript_ref) == [FIRST]


async def test_an_empty_reply_leaves_the_transcript_as_it_was() -> None:
    rig = Rig(reply("end_turn"))
    ref = await rig.put("transcript-1.json", answered())
    out = await rig.run(turn_input(turn_index=1, transcript_ref=ref))
    assert out.transcript_ref == ref
    assert out.tool_call_summaries == []


async def test_a_tool_call_on_an_end_turn_reply_is_neither_run_nor_stored() -> None:
    stray = use("toolu_x", "write_file", path="stray.py", content="x")
    rig = Rig(reply("end_turn", TextBlock(text="All done."), stray))
    out = await rig.run(turn_input())

    assert out.tool_call_summaries == []
    stored = await rig.transcript(out.transcript_ref)
    assert stored[-1] == Message(role="assistant", content=[TextBlock(text="All done.")])


async def test_a_refusal_is_not_stored_and_carries_its_details() -> None:
    details = StopDetails(category="cyber", explanation="The request looks like malware.")
    rig = Rig(reply("refusal", stop_details=details, raw_stop_reason="refusal"))
    out = await rig.run(turn_input())

    assert out.stop_reason == "refusal"
    assert out.stop_details == details
    assert out.raw_stop_reason == "refusal"
    assert await rig.transcript(out.transcript_ref) == [FIRST]


# ---------- pause_turn ----------
async def test_a_paused_turn_hands_its_text_to_the_next_call_and_nothing_else() -> None:
    paused = reply(
        "pause_turn", TextBlock(text="Checking the repo first."), use("toolu_p", path="x")
    )
    done = reply("tool_use", use("toolu_1", "finish", summary="Done."))
    rig = Rig(paused, done)

    first = await rig.run(turn_input())
    assert first.stop_reason == "pause_turn"
    assert first.paused_ref is not None
    assert first.tool_call_summaries == []
    assert await rig.transcript(first.transcript_ref) == [FIRST]
    carried = await rig.blobs.get_json(first.paused_ref, list[TextBlock])
    assert carried == [TextBlock(text="Checking the repo first.")]

    second = await rig.run(
        turn_input(transcript_ref=first.transcript_ref, paused_ref=first.paused_ref)
    )
    # The request ends with the paused text, as an assistant message.
    assert rig.provider.calls[1]["messages"][-1] == Message(
        role="assistant", content=[TextBlock(text="Checking the repo first.")]
    )
    # One assistant message holds the whole turn.
    stored = await rig.transcript(second.transcript_ref)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].content == [
        TextBlock(text="Checking the repo first."),
        use("toolu_1", "finish", summary="Done."),
    ]
    assert second.paused_ref is None


async def test_text_from_two_pauses_is_carried_in_order() -> None:
    rig = Rig(
        reply("pause_turn", TextBlock(text="one")),
        reply("pause_turn", TextBlock(text="two")),
        reply("end_turn", TextBlock(text="three")),
    )
    first = await rig.run(turn_input())
    second = await rig.run(
        turn_input(transcript_ref=first.transcript_ref, paused_ref=first.paused_ref)
    )
    assert second.paused_ref is not None
    carried = await rig.blobs.get_json(second.paused_ref, list[TextBlock])
    assert [b.text for b in carried] == ["one", "two"]

    final = await rig.run(
        turn_input(transcript_ref=second.transcript_ref, paused_ref=second.paused_ref)
    )
    stored = await rig.transcript(final.transcript_ref)
    assert [b.text for b in stored[1].content if isinstance(b, TextBlock)] == [
        "one",
        "two",
        "three",
    ]


async def test_a_pause_with_no_text_carries_nothing_forward() -> None:
    rig = Rig(reply("pause_turn", use("toolu_p", path="x")))
    out = await rig.run(turn_input())
    assert out.stop_reason == "pause_turn"
    assert out.paused_ref is None


# ---------- the size of what crosses the boundary ----------
async def test_arguments_over_16_kb_go_by_reference() -> None:
    big = "x" * 20_000
    rig = Rig(
        reply(
            "tool_use",
            use("toolu_1", "write_file", path="big.txt", content=big),
            use("toolu_2", "read_file", path="big.txt"),
        )
    )
    out = await rig.run(turn_input())

    large, small = out.tool_call_summaries
    assert large.input is None and large.args_ref is not None
    assert small.input == {"path": "big.txt"} and small.args_ref is None
    stored = await rig.blobs.get_json(large.args_ref, dict[str, Any])
    assert stored == {"path": "big.txt", "content": big}
    # The reply and the full list of calls hold the arguments in full.
    assert out.tool_calls_ref is not None
    calls = await rig.blobs.get_json(out.tool_calls_ref, list[ToolCall])
    assert calls[0].input["content"] == big


async def test_the_inline_arguments_of_one_reply_have_a_total_limit() -> None:
    # Twelve calls of 10 KB each are all under 16 KB, but together they would fill the payload.
    blocks = [
        use(f"toolu_{n:02d}", "write_file", path=f"f{n}", content="y" * 10_000) for n in range(12)
    ]
    rig = Rig(reply("tool_use", *blocks))
    out = await rig.run(turn_input())

    summaries = out.tool_call_summaries
    assert [s.id for s in summaries] == [f"toolu_{n:02d}" for n in range(12)]
    inline = [s for s in summaries if s.input is not None]
    by_ref = [s for s in summaries if s.args_ref is not None]
    assert 0 < len(inline) < 12 and len(inline) + len(by_ref) == 12
    assert sum(json_size(s.input) for s in inline) <= acts.INLINE_ARGS_TOTAL_BYTES
    # The calls are all the same size, so the first ones fit and the rest go by reference. The
    # order of the calls is kept either way.
    assert summaries[: len(inline)] == inline
    assert json_size(out) < DEFAULT_LIMIT // 2


async def test_an_input_over_the_payload_limit_fails_without_a_retry_and_without_a_call() -> None:
    rig = Rig()
    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input(hints=["x" * 300_000]))
    assert caught.value.non_retryable
    assert caught.value.type == "PayloadTooLargeError"
    assert rig.provider.calls == []


# ---------- failures ----------
@pytest.mark.parametrize("kind", list(ProviderErrorKind))
async def test_a_provider_error_becomes_an_application_error_of_its_kind(
    kind: ProviderErrorKind,
) -> None:
    rig = Rig(ProviderError(kind, "the provider said no"))
    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input())
    assert caught.value.type == kind.value
    assert caught.value.non_retryable == (not ProviderError(kind, "m").retryable)


async def test_the_retry_after_hint_reaches_temporal() -> None:
    rig = Rig(ProviderError(ProviderErrorKind.RATE_LIMIT, "slow down", retry_after_s=2.0))
    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input())
    assert caught.value.next_retry_delay == timedelta(seconds=2)


async def test_a_billed_call_that_failed_keeps_its_usage_before_the_error_goes_up() -> None:
    billed = Usage(input_tokens=700, output_tokens=0)
    rig = Rig(ProviderError(ProviderErrorKind.SERVER, "body did not parse", usage=billed))
    with pytest.raises(ApplicationError):
        await rig.run(turn_input())

    (row,) = rig.recorder.turns
    assert row.usage == billed
    assert (row.response_ref, row.stop_reason) == (None, None)
    assert row.ended_at is not None
    assert row.attempt == 1


async def test_a_call_that_failed_before_it_was_billed_leaves_no_row() -> None:
    rig = Rig(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))
    with pytest.raises(ApplicationError):
        await rig.run(turn_input())
    assert rig.recorder.turns == []


async def test_the_calls_of_one_turn_have_their_own_rows_and_blobs() -> None:
    rig = Rig(reply("max_tokens", TextBlock(text="cut")), reply("end_turn", TextBlock(text="ok")))
    first = await rig.run(turn_input())
    second = await rig.run(turn_input(transcript_ref=first.transcript_ref, high_output_cap=True))

    assert [(t.attempt, t.stop_reason) for t in rig.recorder.turns] == [
        (1, "max_tokens"),
        (2, "end_turn"),
    ]
    assert first.response_ref is not None and second.response_ref is not None
    assert first.response_ref.key != second.response_ref.key
    # The first response can still be read: the second call did not overwrite it.
    assert (await rig.blobs.get_json(first.response_ref, LLMResponse)).stop_reason == "max_tokens"


async def test_a_transcript_the_api_would_reject_fails_without_a_retry_and_without_a_call() -> None:
    broken = [FIRST, Message(role="assistant", content=[use("toolu_1", path="a.py")])]
    rig = Rig()
    ref = await rig.put("transcript-1.json", broken)
    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input(turn_index=1, transcript_ref=ref))
    assert caught.value.non_retryable
    assert caught.value.type == "TranscriptError"
    assert "toolu_1" in str(caught.value)
    assert rig.provider.calls == []


async def test_a_role_without_a_prompt_fails_without_a_retry() -> None:
    rig = Rig()
    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input(role=Role.PLANNER))
    assert caught.value.non_retryable
    assert "planner" in str(caught.value)
    assert rig.provider.calls == []


# ---------- heartbeats ----------
async def test_the_activity_heartbeats_while_the_model_thinks() -> None:
    async def slow() -> LLMResponse:
        await asyncio.sleep(0.6)
        return reply("end_turn", TextBlock(text="Done."))

    rig = Rig(slow)
    await rig.run(turn_input(), heartbeat_timeout_s=0.3)
    # One at once and one every 0.1 s: the event loop is free while the call waits.
    assert len(rig.beats) >= 4
