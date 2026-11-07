"""llm_turn with the two context tiers (LH-404): eviction, the log, the projection and the guard.

The activity runs inside temporalio's ActivityEnvironment with a fake provider and the memory
blob store. The thresholds depend on the size of the window, so each test works the window out
from the size of the request (`window_for`) and puts the request on one side of a threshold
without counting tokens by hand. The fake provider reports a prompt of as many tokens as the
meter's own rule gives (3.5 characters to a token), unless a test changes `ratio`.
"""

import dataclasses
import math
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import FIRST_USER_MESSAGE, build_system_prompt
from longhaul.blobs.base import blob_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.context.evict import ELIDED_PREFIX, elide_old_outputs
from longhaul.context.log import read_log
from longhaul.context.modes import ContextMode
from longhaul.context.tokens import count_request_chars
from longhaul.contracts import (
    BlobRef,
    LLMResponse,
    Message,
    ProgressNotes,
    ProviderError,
    ProviderErrorKind,
    RepoSettings,
    Role,
    StopReason,
    TaskSpec,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from longhaul.cost.ledger import MemoryLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.llm import LlmActivities
from longhaul.settings import Context
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

SONNET = "claude-sonnet-5-5"
CFG = LoopConfig(model=SONNET, max_turns=10, max_tokens_per_turn=1000, max_tokens_retry=5000)
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
SYSTEM = build_system_prompt(Role.CODER, SPEC)


def context(window: int = 100_000, **changes: Any) -> Context:
    values: dict[str, Any] = {
        "window_tokens": window,
        "compact_at": 0.6,
        "hard_limit": 0.85,
        "keep_recent_turns": 3,
        "elide_tool_outputs_after_turns": 2,
        "tool_output_max_tokens": 400,
    }
    return Context(**{**values, **changes})


def reply(stop: StopReason, *blocks: Any) -> LLMResponse:
    """A reply with no usage. The fake provider fills it in from the request it answers."""
    calls = [
        ToolCall(id=b.id, name=b.name, input=b.input) for b in blocks if isinstance(b, ToolUseBlock)
    ]
    return LLMResponse(
        content=list(blocks),
        tool_calls=calls,
        stop_reason=stop,
        usage=Usage(),
        model=SONNET,
        latency_ms=1,
    )


def use(call_id: str, name: str = "read_file", **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name=name, input=args)


class FakeProvider:
    name = "fake"

    def __init__(self, *steps: LLMResponse | ProviderError) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []
        self.counts: list[dict[str, Any]] = []
        self.counted: int | ProviderError = 0
        self.ratio = 3.5  # characters a token takes, as this provider counts

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, ProviderError):
            raise step
        chars = count_request_chars(kwargs["system"], kwargs["messages"], kwargs["tools"])
        usage = Usage(input_tokens=math.ceil(chars / self.ratio), output_tokens=20)
        return step.model_copy(update={"usage": usage})

    async def count_tokens(self, **kwargs: Any) -> int:
        self.counts.append(kwargs)
        if isinstance(self.counted, ProviderError):
            raise self.counted
        return self.counted


class Rig:
    def __init__(
        self,
        *steps: LLMResponse | ProviderError,
        policy: Context | None = None,
    ) -> None:
        self.provider = FakeProvider(*steps)
        self.blobs = MemoryBlobStore()
        self.recorder = MemoryRecorder()
        self.ledger = MemoryLedger(
            Pricing.load(Path(__file__).resolve().parents[2] / "config" / "pricing.yaml")
        )
        self.activities = LlmActivities(
            self.provider,
            self.blobs,
            self.recorder,
            TOOLS,
            lambda role: CFG,
            ledger=self.ledger,
            context=policy,
        )

    async def run(self, inp: acts.LlmTurnInput, *, attempt: int = 1) -> acts.LlmTurnOutput:
        env = ActivityEnvironment()
        env.info = dataclasses.replace(env.info, attempt=attempt)
        return await env.run(self.activities.llm_turn, inp)

    async def put(self, messages: list[Message], turn: int = 0) -> BlobRef:
        return await self.blobs.put_json(
            messages, blob_key(SPEC.task_id, "turns", turn, "transcript-start.json")
        )

    async def view(self, ref: BlobRef) -> list[Message]:
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


def output(n: int, lines: int = 40) -> str:
    return "\n".join(f"t{n} line {i}: text that takes room in the window" for i in range(lines))


def rounds(count: int, lines: int = 40) -> list[Message]:
    """A transcript that went through `count` tool rounds, ready for the next model call."""
    messages = [FIRST]
    for n in range(1, count + 1):
        messages += [
            Message(role="assistant", content=[use(f"call-{n}", path=f"f{n}.py")]),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id=f"call-{n}", content=output(n, lines))],
            ),
        ]
    return messages


def estimate(messages: list[Message]) -> int:
    """Tokens of a request by the meter's rule before it has seen a reply."""
    return math.ceil(count_request_chars(SYSTEM, messages, TOOLS) / 3.5)


def window_for(messages: list[Message], share: float) -> int:
    """The window in which the request takes `share` of the room."""
    return round(estimate(messages) / share)


def results(messages: list[Message]) -> list[str]:
    return [b.content for m in messages for b in m.content if isinstance(b, ToolResultBlock)]


# ---------- tier 1 ----------
async def test_old_tool_results_are_elided_in_the_request_and_in_the_view() -> None:
    rig = Rig(reply("tool_use", use("call-9")), policy=context())
    ref = await rig.put(rounds(8))

    out = await rig.run(turn_input(turn_index=8, transcript_ref=ref))

    sent = results(rig.provider.calls[0]["messages"])
    stored = results(await rig.view(out.transcript_ref))
    assert sent == stored
    assert [text.startswith(ELIDED_PREFIX) for text in sent] == [True] * 6 + [False] * 2
    assert sent[0] == "[elided: read_file output, 40 lines, handle out_call-1]"


async def test_the_elided_text_is_the_same_bytes_on_the_next_call() -> None:
    # The cache holds the prefix of the request, so a stub made on one turn is reused on the next.
    rig = Rig(reply("tool_use", use("call-9")), reply("tool_use", use("call-10")), policy=context())
    ref = await rig.put(rounds(8))
    first = await rig.run(turn_input(turn_index=8, transcript_ref=ref))
    grown = [
        *await rig.view(first.transcript_ref),
        Message(role="user", content=[ToolResultBlock(tool_use_id="call-9", content=output(9))]),
    ]
    second = await rig.run(turn_input(turn_index=9, transcript_ref=await rig.put(grown, turn=9)))

    before, after = (
        results(await rig.view(first.transcript_ref)),
        results(await rig.view(second.transcript_ref)),
    )
    assert after[:6] == before[:6]
    assert after[6].startswith(ELIDED_PREFIX)  # one more result has aged out


async def test_without_a_context_policy_nothing_is_elided() -> None:
    rig = Rig(reply("tool_use", use("call-9")))
    ref = await rig.put(rounds(8))

    out = await rig.run(turn_input(turn_index=8, transcript_ref=ref))

    assert results(rig.provider.calls[0]["messages"]) == results(rounds(8))
    assert out.context_decision == "ok"
    assert out.request_tokens == 0


async def test_a_hint_is_shown_after_the_elided_results() -> None:
    rig = Rig(reply("tool_use", use("call-9")), policy=context())
    ref = await rig.put(rounds(5))

    await rig.run(turn_input(turn_index=5, transcript_ref=ref, hints=["alice: use pytest -x"]))

    last = rig.provider.calls[0]["messages"][-1]
    assert last.content[-1] == TextBlock(text="Hint from alice: use pytest -x")


# ---------- the log ----------
async def test_the_log_gets_the_reply_of_the_turn() -> None:
    # The log is read from turn 0, so the turn that is logged here is turn 0 with a view that
    # already holds earlier rounds.
    rig = Rig(reply("tool_use", use("call-9")), policy=context())
    ref = await rig.put(rounds(8))

    await rig.run(turn_input(turn_index=0, transcript_ref=ref))

    log = await read_log(rig.blobs, SPEC.task_id)
    assert log == [Message(role="assistant", content=[use("call-9")])]


async def test_the_first_turn_logs_the_first_message_too() -> None:
    rig = Rig(reply("tool_use", use("call-1")), policy=context())

    await rig.run(turn_input())

    log = await read_log(rig.blobs, SPEC.task_id)
    assert log == [FIRST, Message(role="assistant", content=[use("call-1")])]


async def test_a_hint_is_in_the_log_with_the_reply_it_came_with() -> None:
    rig = Rig(reply("tool_use", use("call-9")), policy=context())
    ref = await rig.put(rounds(2))

    await rig.run(turn_input(turn_index=0, transcript_ref=ref, hints=["alice: use pytest -x"]))

    log = await read_log(rig.blobs, SPEC.task_id)
    assert log == [
        Message(role="user", content=[TextBlock(text="Hint from alice: use pytest -x")]),
        Message(role="assistant", content=[use("call-9")]),
    ]


async def test_the_log_is_written_without_a_context_policy_too() -> None:
    rig = Rig(reply("tool_use", use("call-1")))

    await rig.run(turn_input())

    assert len(await read_log(rig.blobs, SPEC.task_id)) == 2


async def test_a_retried_turn_does_not_put_its_reply_in_the_log_twice() -> None:
    rig = Rig(reply("tool_use", use("call-1")), reply("tool_use", use("call-1")), policy=context())

    await rig.run(turn_input(), attempt=1)
    await rig.run(turn_input(), attempt=2)

    log = await read_log(rig.blobs, SPEC.task_id)
    assert [m.role for m in log] == ["user", "assistant"]


async def test_a_reply_that_is_not_stored_is_not_logged() -> None:
    rig = Rig(reply("max_tokens", TextBlock(text="Writing it all")), policy=context())

    await rig.run(turn_input())

    assert await read_log(rig.blobs, SPEC.task_id) == []


# ---------- tier 2: the decision for the next turn ----------
@pytest.mark.parametrize(
    ("share", "reserve", "decision"),
    [
        (0.30, 0.00, "ok"),
        (0.50, 0.05, "ok"),  # 0.55 in all: under compact_at
        (0.50, 0.15, "compact"),  # 0.65: past compact_at, short of hard_limit
        (0.70, 0.20, "force"),  # 0.90: past hard_limit
    ],
)
async def test_the_activity_says_what_to_do_before_the_next_turn(
    share: float, reserve: float, decision: str
) -> None:
    # `share` is how much of the window the request takes. The reply asks for one tool call, and
    # its result may take `reserve` of the window (tool_output_max_tokens).
    messages = rounds(1, lines=3)
    window = window_for(messages, share)
    cap = round(window * reserve)
    calls = [use("c9")] if reserve else []
    rig = Rig(
        reply("tool_use" if calls else "end_turn", *calls),
        policy=context(window, tool_output_max_tokens=max(cap, 1)),
    )
    ref = await rig.put(messages)

    out = await rig.run(turn_input(turn_index=1, transcript_ref=ref))

    assert out.context_decision == decision


async def test_more_calls_leave_room_for_more_results() -> None:
    messages = rounds(1, lines=3)
    window = window_for(messages, 0.4)
    policy = context(window, tool_output_max_tokens=round(window * 0.08))
    decisions = []
    for count in (1, 2, 3):
        calls = [use(f"c{n}") for n in range(count)]
        rig = Rig(reply("tool_use", *calls), policy=policy)
        out = await rig.run(turn_input(turn_index=1, transcript_ref=await rig.put(messages)))
        decisions.append(out.context_decision)

    # 0.40 and 8 percent for each result: 0.48, 0.56 and 0.64.
    assert decisions == ["ok", "ok", "compact"]


async def test_a_finish_call_leaves_no_room_for_a_result() -> None:
    messages = rounds(1, lines=3)
    window = window_for(messages, 0.5)
    rig = Rig(
        reply("tool_use", use("c1", "finish", summary="Done.")),
        policy=context(window, tool_output_max_tokens=window),
    )

    out = await rig.run(turn_input(turn_index=1, transcript_ref=await rig.put(messages)))

    assert out.context_decision == "ok"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("update_step", {"step_id": "s1", "status": "done"}),
        ("submit_for_review", {"summary": "Done."}),
        ("ask_human", {"question": "Which one?"}),
    ],
)
async def test_a_step_tool_call_leaves_no_room_for_a_result(
    name: str, args: dict[str, str]
) -> None:
    # LH-503: the workflow answers these three itself, with a line of text, as it does finish.
    messages = rounds(1, lines=3)
    window = window_for(messages, 0.5)
    rig = Rig(
        reply("tool_use", use("c1", name, **args)),
        policy=context(window, tool_output_max_tokens=window),
    )

    out = await rig.run(turn_input(turn_index=1, transcript_ref=await rig.put(messages)))

    assert out.context_decision == "ok"


async def test_the_next_request_is_projected_with_one_more_turn_elided() -> None:
    # Three big results are in the view and two are kept. The request that is sent has the first
    # one as a stub and takes 0.70 of the window, which would call for a compaction. The next
    # request will have the second one as a stub too, and is well under 0.60.
    messages = rounds(3, lines=200)
    sent = elide_old_outputs(messages, keep_turns=2)
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context(window_for(sent, 0.7)))

    out = await rig.run(turn_input(turn_index=3, transcript_ref=await rig.put(messages)))

    assert out.request_tokens == estimate(sent)
    assert out.context_decision == "ok"


# ---------- the guard in front of the call ----------
async def test_a_request_over_the_hard_limit_is_not_sent() -> None:
    messages = rounds(2, lines=40)
    rig = Rig(
        reply("end_turn", TextBlock(text="Done.")),
        policy=context(window_for(messages, 0.9)),
    )

    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert caught.value.type == ProviderErrorKind.CONTEXT_TOO_LONG.value
    assert caught.value.non_retryable is True
    assert rig.provider.calls == []
    assert rig.recorder.turns == []  # nothing was billed
    assert rig.ledger.entries == []


async def test_the_refusal_names_the_projection_and_the_limit() -> None:
    messages = rounds(2, lines=40)
    window = window_for(messages, 0.9)
    rig = Rig(policy=context(window))

    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert f"about {estimate(messages)} tokens" in str(caught.value)
    assert f"hard limit of {int(window * 0.85)} tokens" in str(caught.value)


async def test_the_request_just_under_the_hard_limit_is_sent() -> None:
    messages = rounds(2, lines=40)
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context(window_for(messages, 0.8)))

    out = await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert len(rig.provider.calls) == 1
    assert out.request_tokens == estimate(messages)


async def test_the_projection_of_the_request_is_returned() -> None:
    messages = rounds(2, lines=5)
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context())

    out = await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    # No reply has calibrated the meter yet, so the first projection is the default rule.
    assert out.request_tokens == estimate(messages)


async def test_the_meter_learns_from_the_replies_of_a_task() -> None:
    # The provider counts seven characters to a token. After the first reply the meter knows,
    # and the second request is projected at that ratio and not at 3.5.
    messages = rounds(2, lines=5)
    rig = Rig(reply("tool_use", use("call-3")), reply("tool_use", use("call-4")), policy=context())
    rig.provider.ratio = 7.0
    first = await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))
    grown = [
        *await rig.view(first.transcript_ref),
        Message(role="user", content=[ToolResultBlock(tool_use_id="call-3", content="x")]),
    ]

    second = await rig.run(turn_input(turn_index=3, transcript_ref=await rig.put(grown, turn=3)))

    assert first.request_tokens == estimate(messages)
    assert second.request_tokens < estimate(messages) * 0.6


async def test_one_task_does_not_calibrate_the_meter_of_another() -> None:
    messages = rounds(2, lines=5)
    rig = Rig(reply("tool_use", use("call-3")), reply("tool_use", use("call-3")), policy=context())
    rig.provider.ratio = 7.0
    await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    other = SPEC.model_copy(update={"task_id": "lh-acme-calc-43"})
    out = await rig.run(
        turn_input(task_id=other.task_id, spec=other, turn_index=2, transcript_ref=None)
    )

    assert out.request_tokens == estimate([FIRST])


async def test_the_exact_count_replaces_the_estimate_close_to_the_threshold() -> None:
    messages = rounds(2, lines=40)
    window = window_for(messages, 0.7)  # inside the band, under the hard limit
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context(window))
    rig.provider.counted = round(window * 0.8)

    out = await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert out.request_tokens == round(window * 0.8)
    assert len(rig.provider.counts) == 1


async def test_the_exact_count_is_asked_with_the_request_that_is_sent() -> None:
    messages = rounds(2, lines=40)
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context(window_for(messages, 0.7)))
    rig.provider.counted = 1

    await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    (counted,) = rig.provider.counts
    (sent,) = rig.provider.calls
    assert counted["system"] == sent["system"]
    assert counted["messages"] == sent["messages"]
    assert counted["tools"] == sent["tools"]
    assert counted["model"] == sent["model"]
    assert counted["options"] == sent["options"]


async def test_an_exact_count_over_the_hard_limit_stops_the_call() -> None:
    messages = rounds(2, lines=40)
    window = window_for(messages, 0.7)
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context(window))
    rig.provider.counted = round(window * 0.9)  # the estimate was fine and the real count is not

    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert caught.value.type == ProviderErrorKind.CONTEXT_TOO_LONG.value
    assert rig.provider.calls == []


async def test_far_from_the_threshold_the_estimate_is_enough() -> None:
    messages = rounds(2, lines=5)
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context(window_for(messages, 0.1)))

    await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert rig.provider.counts == []


@pytest.mark.parametrize("counted", [ProviderError(ProviderErrorKind.OVERLOADED, "busy"), 0])
async def test_a_count_that_fails_or_is_empty_leaves_the_estimate(
    counted: ProviderError | int,
) -> None:
    messages = rounds(2, lines=40)
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context(window_for(messages, 0.7)))
    rig.provider.counted = counted

    out = await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert out.request_tokens == estimate(messages)


# ---------- the prompt stays as it was ----------
async def test_the_notes_do_not_change_the_system_prompt() -> None:
    # D7: the system prompt is the same bytes for the life of a view. The notes reach the model
    # in the first message, where compaction put them.
    notes = ProgressNotes(
        goal="fix add()",
        plan_status=[],
        files_touched=[],
        decisions=[],
        failing_tests=[],
        open_hypotheses=[],
        tried_and_failed=[],
        covers_turns_up_to=3,
    )
    rig = Rig(reply("end_turn", TextBlock(text="a")), reply("end_turn", TextBlock(text="b")))
    ref = await rig.put(rounds(2, lines=3))

    await rig.run(turn_input(turn_index=2, transcript_ref=ref))
    await rig.run(turn_input(turn_index=2, transcript_ref=ref, notes=notes))

    first, second = rig.provider.calls
    assert first["system"] == second["system"] == SYSTEM


# ---------- a policy that cannot work stops the worker, not the first turn ----------
def test_a_policy_that_never_keeps_a_result_is_refused() -> None:
    with pytest.raises(ValueError, match="elide_tool_outputs_after_turns"):
        Rig(policy=context(elide_tool_outputs_after_turns=0))


def test_thresholds_that_cross_are_refused() -> None:
    with pytest.raises(ValueError, match="compact_at"):
        Rig(policy=context(compact_at=0.9, hard_limit=0.8))


# ---------- the modes (LH-406) ----------
@pytest.mark.parametrize(
    ("mode", "elided"),
    [
        (ContextMode.FULL, True),
        (ContextMode.NO_COMPACTION, True),
        (ContextMode.NONE, False),
    ],
)
async def test_the_modes_with_tier_one_elide_and_none_does_not(
    mode: ContextMode, elided: bool
) -> None:
    rig = Rig(reply("tool_use", use("call-9")), policy=context(mode=mode))
    ref = await rig.put(rounds(8))

    out = await rig.run(turn_input(turn_index=8, transcript_ref=ref))

    sent = results(rig.provider.calls[0]["messages"])
    assert results(await rig.view(out.transcript_ref)) == sent
    assert any(text.startswith(ELIDED_PREFIX) for text in sent) is elided
    if not elided:
        assert sent == results(rounds(8))


@pytest.mark.parametrize(
    ("share", "reserve", "full"),
    [(0.50, 0.15, "compact"), (0.70, 0.20, "force")],
)
@pytest.mark.parametrize(
    ("mode", "wanted"),
    [(ContextMode.FULL, None), (ContextMode.NO_COMPACTION, "ok"), (ContextMode.NONE, "ok")],
)
async def test_only_the_full_mode_asks_for_a_compaction(
    mode: ContextMode, wanted: str | None, share: float, reserve: float, full: str
) -> None:
    # The same request calls for a compaction (or a forced one) in the full mode and for
    # nothing in the others: a workflow that is told "ok" does not compact.
    messages = rounds(1, lines=3)
    window = window_for(messages, share)
    policy = context(window, tool_output_max_tokens=round(window * reserve), mode=mode)
    rig = Rig(reply("tool_use", use("c9")), policy=policy)

    out = await rig.run(turn_input(turn_index=1, transcript_ref=await rig.put(messages)))

    assert out.context_decision == (wanted or full)


@pytest.mark.parametrize("mode", list(ContextMode))
async def test_every_mode_refuses_a_request_over_the_hard_limit(mode: ContextMode) -> None:
    # `none` drops both tiers, and the guard stays: it is what ends the task cleanly.
    messages = rounds(2, lines=40)
    rig = Rig(
        reply("end_turn", TextBlock(text="Done.")),
        policy=context(window_for(messages, 0.9), mode=mode),
    )

    with pytest.raises(ApplicationError) as caught:
        await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert caught.value.type == ProviderErrorKind.CONTEXT_TOO_LONG.value
    assert caught.value.non_retryable is True
    assert rig.provider.calls == []


@pytest.mark.parametrize(
    ("config", "asked", "elided"),
    [
        (ContextMode.NONE, ContextMode.FULL, True),
        (ContextMode.NONE, ContextMode.NO_COMPACTION, True),
        (ContextMode.FULL, ContextMode.NONE, False),
        (ContextMode.NO_COMPACTION, ContextMode.NONE, False),
    ],
)
async def test_the_mode_of_the_run_wins_over_the_config(
    config: ContextMode, asked: ContextMode, elided: bool
) -> None:
    rig = Rig(reply("tool_use", use("call-9")), policy=context(mode=config))
    ref = await rig.put(rounds(8))

    await rig.run(turn_input(turn_index=8, transcript_ref=ref, context_mode=asked))

    sent = results(rig.provider.calls[0]["messages"])
    assert any(text.startswith(ELIDED_PREFIX) for text in sent) is elided


async def test_the_mode_of_the_run_decides_whether_the_next_turn_compacts() -> None:
    messages = rounds(1, lines=3)
    window = window_for(messages, 0.5)
    policy = context(window, tool_output_max_tokens=round(window * 0.15), mode=ContextMode.NONE)
    rig = Rig(reply("tool_use", use("c9")), policy=policy)

    out = await rig.run(
        turn_input(
            turn_index=1,
            transcript_ref=await rig.put(messages),
            context_mode=ContextMode.FULL,
        )
    )

    assert out.context_decision == "compact"


async def test_a_mode_without_a_context_policy_changes_nothing() -> None:
    rig = Rig(reply("tool_use", use("call-9")))
    ref = await rig.put(rounds(8))

    out = await rig.run(turn_input(turn_index=8, transcript_ref=ref, context_mode=ContextMode.FULL))

    assert results(rig.provider.calls[0]["messages"]) == results(rounds(8))
    assert out.context_decision == "ok"


# ---------- the share of the window (LH-406) ----------
async def test_the_share_of_the_window_that_the_request_took_is_recorded() -> None:
    messages = rounds(2, lines=5)
    window = 20_000
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), policy=context(window))

    out = await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert out.request_tokens > 0
    assert rig.recorder.max_fractions == {SPEC.task_id: pytest.approx(out.request_tokens / window)}


async def test_a_smaller_request_does_not_lower_the_recorded_share() -> None:
    big, small = rounds(4, lines=20), rounds(1, lines=2)
    rig = Rig(
        reply("end_turn", TextBlock(text="Done.")),
        reply("end_turn", TextBlock(text="Done.")),
        policy=context(20_000),
    )

    first = await rig.run(turn_input(turn_index=4, transcript_ref=await rig.put(big, turn=4)))
    second = await rig.run(turn_input(turn_index=5, transcript_ref=await rig.put(small, turn=5)))

    assert second.request_tokens < first.request_tokens
    assert rig.recorder.max_fractions[SPEC.task_id] == pytest.approx(first.request_tokens / 20_000)


async def test_a_request_that_was_not_sent_is_not_recorded() -> None:
    messages = rounds(2, lines=40)
    rig = Rig(policy=context(window_for(messages, 0.9)))

    with pytest.raises(ApplicationError):
        await rig.run(turn_input(turn_index=2, transcript_ref=await rig.put(messages)))

    assert rig.recorder.max_fractions == {}


async def test_without_a_context_policy_no_share_is_recorded() -> None:
    rig = Rig(reply("end_turn", TextBlock(text="Done.")))

    await rig.run(turn_input())

    assert rig.recorder.max_fractions == {}


async def test_a_retried_turn_records_the_same_share() -> None:
    # Recording is a maximum, so a second run of the activity cannot count the request twice.
    rig = Rig(
        reply("end_turn", TextBlock(text="Done.")),
        reply("end_turn", TextBlock(text="Done.")),
        policy=context(20_000),
    )

    await rig.run(turn_input(), attempt=1)
    once = dict(rig.recorder.max_fractions)
    await rig.run(turn_input(), attempt=2)

    assert rig.recorder.max_fractions == once
