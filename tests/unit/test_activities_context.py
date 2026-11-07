"""The compact activity (LH-404): the older turns of a view become progress notes.

The activity runs inside temporalio's ActivityEnvironment with a fake compactor, the memory blob
store and the memory ledger on the checked-in prices. The model calls and the ledger are the
parts that matter: every call the compactor makes has a row of its own (invariant I6).
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
from longhaul.agent.turns import show_hints, validate_pairs
from longhaul.blobs.base import blob_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.context.compact import NO_NOTES, NOTES_HEADING, NOTES_TOOL_NAME
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
from longhaul.providers.base import read_turn_offset, turn_index
from longhaul.settings import Context
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"
PRICES = Pricing.load(Path(__file__).resolve().parents[2] / "config" / "pricing.yaml")
SPEC = TaskSpec(
    task_id="lh-acme-calc-42",
    repo="acme/calc",
    issue_number=42,
    title="add() subtracts",
    body="add(2, 3) returns -1",
    settings=RepoSettings(repo="acme/calc"),
)
TOOLS = default_registry().specs()
SYSTEM = build_system_prompt(Role.CODER, SPEC)
FIRST = Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])
POLICY = Context(
    window_tokens=8_000,
    compact_at=0.6,
    hard_limit=0.85,
    keep_recent_turns=3,
    elide_tool_outputs_after_turns=2,
    tool_output_max_tokens=400,
)
NOTES = ProgressNotes(
    goal="Fix add() in calc",
    plan_status=[],
    files_touched=["src/calc/__init__.py"],
    decisions=["Keep the public API"],
    failing_tests=["tests/test_calc.py::test_add"],
    open_hypotheses=[],
    tried_and_failed=["Casting both operands to int"],
    covers_turns_up_to=99,  # the activity replaces this with the turns it folded
)
USAGE = Usage(input_tokens=900, output_tokens=120)
LONG_PROMPT = Usage(input_tokens=150_000, output_tokens=200)  # past Haiku's 100,000 token line
SHORT_PROMPT = Usage(input_tokens=90_000, output_tokens=300)


def config_for(role: Role) -> LoopConfig:
    model = HAIKU if role is Role.COMPACTOR else SONNET
    return LoopConfig(model=model, effort="low" if role is Role.COMPACTOR else "medium")


def notes_reply(notes: ProgressNotes = NOTES, usage: Usage = USAGE, call_id: str = "n1") -> Any:
    call = ToolUseBlock(id=call_id, name=NOTES_TOOL_NAME, input=notes.model_dump(mode="json"))
    return LLMResponse(
        content=[call],
        tool_calls=[ToolCall(id=call.id, name=call.name, input=call.input)],
        stop_reason="tool_use",
        usage=usage,
        model=HAIKU,
        latency_ms=1,
    )


def chatter(usage: Usage = USAGE) -> LLMResponse:
    return LLMResponse(
        content=[TextBlock(text="The add function was fixed.")],
        tool_calls=[],
        stop_reason="end_turn",
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
    def __init__(
        self,
        *steps: LLMResponse | ProviderError,
        ledger: MemoryLedger | None = None,
        policy: Context = POLICY,
    ) -> None:
        self.provider = FakeProvider(*steps)
        self.blobs = MemoryBlobStore()
        self.ledger = ledger or MemoryLedger(PRICES)
        self.activities = ContextActivities(
            self.provider, self.blobs, TOOLS, config_for, policy, ledger=self.ledger
        )

    async def run(self, inp: acts.CompactInput, *, attempt: int = 1) -> acts.CompactOutput:
        env = ActivityEnvironment()
        env.info = dataclasses.replace(env.info, attempt=attempt)
        return await env.run(self.activities.compact, inp)

    async def put(self, messages: list[Message]) -> BlobRef:
        return await self.blobs.put_json(
            messages, blob_key(SPEC.task_id, "turns", 0, "transcript-start.json")
        )

    async def view(self, ref: BlobRef) -> list[Message]:
        return await self.blobs.get_json(ref, list[Message])


def use(call_id: str, **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name="run_command", input=args)


def rounds(count: int, lines: int = 30) -> list[Message]:
    messages = [FIRST]
    for n in range(1, count + 1):
        out = "\n".join(f"t{n} line {i}" for i in range(lines))
        messages += [
            Message(role="assistant", content=[use(f"call-{n}", command=f"step {n}")]),
            Message(role="user", content=[ToolResultBlock(tool_use_id=f"call-{n}", content=out)]),
        ]
    return messages


def compact_input(ref: BlobRef, **fields: Any) -> acts.CompactInput:
    values: dict[str, Any] = {
        "task_id": SPEC.task_id,
        "turn_index": 8,
        "role": Role.CODER,
        "spec": SPEC,
        "transcript_ref": ref,
    }
    return acts.CompactInput(**{**values, **fields})


def assistants(messages: list[Message]) -> int:
    return sum(m.role == "assistant" for m in messages)


def first_text(messages: list[Message]) -> str:
    return "\n".join(b.text for b in messages[0].content if isinstance(b, TextBlock))


# ---------- the compaction ----------
async def test_older_turns_become_notes_and_the_recent_ones_are_kept() -> None:
    rig = Rig(notes_reply())
    ref = await rig.put(rounds(8))

    out = await rig.run(compact_input(ref))

    view = await rig.view(out.transcript_ref)
    assert out.status == "ok"
    assert assistants(view) == POLICY.keep_recent_turns
    assert view[1:] == rounds(8)[-6:]  # the last three turns, untouched
    assert NOTES_HEADING in first_text(view)
    assert view[0].content[0] == TextBlock(text=FIRST_USER_MESSAGE)
    validate_pairs(view)


async def test_the_notes_are_returned_with_the_turns_they_cover() -> None:
    rig = Rig(notes_reply())
    ref = await rig.put(rounds(8))

    out = await rig.run(compact_input(ref))

    assert out.notes is not None
    assert out.notes.goal == NOTES.goal
    assert out.notes.covers_turns_up_to == 5  # turns 1 to 5 were folded
    assert out.folded_turns == 5
    assert turn_index(await rig.view(out.transcript_ref)) == 8  # the task is still on turn 8
    assert read_turn_offset(await rig.view(out.transcript_ref)) == 5


async def test_a_forced_compaction_keeps_two_turns() -> None:
    rig = Rig(notes_reply())
    ref = await rig.put(rounds(8))

    out = await rig.run(compact_input(ref, forced=True))

    assert assistants(await rig.view(out.transcript_ref)) == acts.FORCED_KEEP_RECENT_TURNS
    assert out.folded_turns == 6


async def test_the_compactor_is_asked_as_the_compactor() -> None:
    rig = Rig(notes_reply())

    await rig.run(compact_input(await rig.put(rounds(8))))

    (call,) = rig.provider.calls
    assert call["model"] == HAIKU
    assert call["system"].startswith("Role: compactor")
    assert call["options"].effort == "low"
    assert call["options"].force_tool == NOTES_TOOL_NAME


async def test_the_compactor_reads_the_turns_that_go_and_not_the_ones_that_stay() -> None:
    rig = Rig(notes_reply())

    await rig.run(compact_input(await rig.put(rounds(8))))

    (call,) = rig.provider.calls
    prompt = call["messages"][0].content[0].text
    assert "[turn 1]" in prompt and "[turn 5]" in prompt
    assert "[turn 6]" not in prompt


async def test_the_first_round_gets_the_task_and_a_later_round_gets_the_notes() -> None:
    first = Rig(notes_reply())
    await first.run(compact_input(await first.put(rounds(8))))
    later = Rig(notes_reply())
    await later.run(compact_input(await later.put(rounds(8)), notes=NOTES))

    first_prompt = first.provider.calls[0]["messages"][0].content[0].text
    later_prompt = later.provider.calls[0]["messages"][0].content[0].text
    assert "## Task statement" in first_prompt and "add(2, 3) returns -1" in first_prompt
    assert "## Task statement" not in later_prompt
    assert NOTES.goal in later_prompt


async def test_a_tried_and_failed_entry_of_an_earlier_round_survives() -> None:
    dropped = NOTES.model_copy(update={"tried_and_failed": []})
    rig = Rig(notes_reply(dropped))
    ref = await rig.put(rounds(8))

    out = await rig.run(compact_input(ref, notes=NOTES))

    assert out.notes is not None
    assert out.notes.tried_and_failed == NOTES.tried_and_failed


# ---------- tokens ----------
async def test_the_tokens_before_and_after_are_those_of_the_whole_request() -> None:
    rig = Rig(notes_reply())
    messages = rounds(8)
    ref = await rig.put(messages)

    out = await rig.run(compact_input(ref))

    view = await rig.view(out.transcript_ref)
    assert out.tokens_before == math.ceil(count_request_chars(SYSTEM, messages, TOOLS) / 3.5)
    assert out.tokens_after == math.ceil(count_request_chars(SYSTEM, view, TOOLS) / 3.5)
    assert out.tokens_after < out.tokens_before


@pytest.mark.phase6
async def test_the_tokens_include_the_memory_block_that_the_system_prompt_carries() -> None:
    # llm_turn sends the block with every request, so the compactor must count it too, or it
    # would report a request smaller than the one the model turn projected.
    block = "Notes from past work on this repo.\n\n```text\n" + "- pytest -q\n" * 300 + "```"
    rig = Rig(notes_reply())
    messages = rounds(8)
    ref = await rig.put(messages)
    memory_ref = await rig.blobs.put(
        block.encode(), key=blob_key(SPEC.task_id, "memory", 0, "b.txt")
    )

    out = await rig.run(compact_input(ref, memory_ref=memory_ref))

    system = build_system_prompt(Role.CODER, SPEC, block)
    view = await rig.view(out.transcript_ref)
    assert out.tokens_before == math.ceil(count_request_chars(system, messages, TOOLS) / 3.5)
    assert out.tokens_after == math.ceil(count_request_chars(system, view, TOOLS) / 3.5)
    assert out.tokens_before > math.ceil(count_request_chars(SYSTEM, messages, TOOLS) / 3.5)


# ---------- nothing to cut ----------
async def test_a_view_with_no_older_turns_is_left_as_it_is() -> None:
    rig = Rig()
    ref = await rig.put(rounds(3))

    out = await rig.run(compact_input(ref))

    assert out.status == "unchanged"
    assert out.transcript_ref == ref
    assert out.notes is None
    assert out.folded_turns == 0
    assert out.tokens_before == out.tokens_after
    assert rig.provider.calls == []
    assert rig.ledger.entries == []


async def test_a_forced_compaction_of_a_short_view_is_left_as_it_is() -> None:
    rig = Rig()

    out = await rig.run(compact_input(await rig.put(rounds(2)), forced=True))

    assert out.status == "unchanged"


async def test_the_notes_come_back_unchanged_when_there_is_nothing_to_cut() -> None:
    rig = Rig()

    out = await rig.run(compact_input(await rig.put(rounds(2)), notes=NOTES))

    assert out.notes == NOTES


# ---------- F14: a compactor that does not give notes ----------
async def test_a_reply_that_needs_a_repair_is_still_applied() -> None:
    rig = Rig(chatter(), notes_reply())

    out = await rig.run(compact_input(await rig.put(rounds(8))))

    assert out.status == "repaired"
    assert out.notes is not None and out.notes.goal == NOTES.goal


async def test_a_fallback_keeps_the_old_notes_and_cuts_half_as_much() -> None:
    rig = Rig(chatter(), chatter())
    ref = await rig.put(rounds(8))

    out = await rig.run(compact_input(ref, notes=NOTES))

    view = await rig.view(out.transcript_ref)
    assert out.status == "fallback"
    assert out.notes == NOTES
    # The cut at 3 kept turns is at message 11 (5 turns folded). Half of it is message 5, which
    # folds 2 turns.
    assert out.folded_turns == 2
    assert assistants(view) == 6
    validate_pairs(view)


async def test_a_fallback_that_halves_the_cut_to_nothing_leaves_the_view_as_it_is() -> None:
    # Four turns with three to keep fold one turn. Half of that cut is no cut at all. The two
    # calls were made, so they are on the ledger, and the workflow is not failed.
    rig = Rig(chatter(LONG_PROMPT), chatter(SHORT_PROMPT))
    ref = await rig.put(rounds(4))

    out = await rig.run(compact_input(ref, notes=NOTES))

    assert out.status == "fallback"
    assert out.transcript_ref == ref
    assert out.notes == NOTES
    assert out.folded_turns == 0
    assert out.tokens_before == out.tokens_after
    assert [r.usage for r in rig.ledger.entries] == [LONG_PROMPT, SHORT_PROMPT]
    assert out.cost_usd == pytest.approx(float(sum(r.usd for r in rig.ledger.entries)))


async def test_a_fallback_on_the_first_round_says_that_no_notes_were_kept() -> None:
    rig = Rig(chatter(), chatter())

    out = await rig.run(compact_input(await rig.put(rounds(8))))

    view = await rig.view(out.transcript_ref)
    assert out.status == "fallback"
    assert out.notes is None
    assert NO_NOTES in first_text(view)


# ---------- I6: a row for every call ----------
async def test_every_call_has_a_ledger_row_of_its_own() -> None:
    # The two calls are on different sides of Haiku's price line. Added together they would be
    # priced as one long prompt, which neither of them was.
    rig = Rig(chatter(LONG_PROMPT), notes_reply(usage=SHORT_PROMPT))

    out = await rig.run(compact_input(await rig.put(rounds(8))), attempt=2)

    rows = rig.ledger.entries
    assert [(r.model, r.usage, r.turn_index, r.attempt) for r in rows] == [
        (HAIKU, LONG_PROMPT, 8, 2),
        (HAIKU, SHORT_PROMPT, 8, 2),
    ]
    assert all(r.task_id == SPEC.task_id and r.repo == "acme/calc" for r in rows)
    assert [r.usd for r in rows] == [
        PRICES.cost(LONG_PROMPT, HAIKU),
        PRICES.cost(SHORT_PROMPT, HAIKU),
    ]
    summed = Usage(
        input_tokens=LONG_PROMPT.input_tokens + SHORT_PROMPT.input_tokens,
        output_tokens=LONG_PROMPT.output_tokens + SHORT_PROMPT.output_tokens,
    )
    assert sum(r.usd for r in rows) < PRICES.cost(summed, HAIKU)
    assert out.cost_usd == pytest.approx(float(sum(r.usd for r in rows)))


async def test_calls_made_before_a_failure_are_on_the_ledger() -> None:
    busy = ProviderError(ProviderErrorKind.OVERLOADED, "busy", usage=SHORT_PROMPT)
    rig = Rig(chatter(LONG_PROMPT), busy)

    with pytest.raises(ApplicationError) as caught:
        await rig.run(compact_input(await rig.put(rounds(8))))

    assert caught.value.type == ProviderErrorKind.OVERLOADED.value
    assert caught.value.non_retryable is False  # Temporal asks again
    assert [r.usage for r in rig.ledger.entries] == [LONG_PROMPT, SHORT_PROMPT]


async def test_a_call_that_failed_before_it_was_billed_has_no_row() -> None:
    rig = Rig(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))

    with pytest.raises(ApplicationError):
        await rig.run(compact_input(await rig.put(rounds(8))))

    assert rig.ledger.entries == []


@pytest.mark.parametrize(
    ("kind", "retried"),
    [
        (ProviderErrorKind.RATE_LIMIT, True),
        (ProviderErrorKind.SERVER, True),
        (ProviderErrorKind.TIMEOUT, True),
        (ProviderErrorKind.AUTH, False),
        (ProviderErrorKind.CONTEXT_TOO_LONG, False),
    ],
)
async def test_a_provider_error_keeps_its_kind_and_its_retry_advice(
    kind: ProviderErrorKind, retried: bool
) -> None:
    rig = Rig(ProviderError(kind, "no"))

    with pytest.raises(ApplicationError) as caught:
        await rig.run(compact_input(await rig.put(rounds(8))))

    assert caught.value.type == kind.value
    assert caught.value.non_retryable is (not retried)


async def test_a_model_without_a_price_is_not_retried() -> None:
    # Asking again would bill the call again, and none of the calls could be priced.
    rig = Rig(notes_reply(), ledger=MemoryLedger(Pricing({})))

    with pytest.raises(ApplicationError) as caught:
        await rig.run(compact_input(await rig.put(rounds(8))))

    assert caught.value.type == "PricingError"
    assert caught.value.non_retryable is True


# ---------- the view that is stored ----------
async def test_the_view_is_stored_under_the_turn_and_the_attempt() -> None:
    rig = Rig(notes_reply())

    out = await rig.run(compact_input(await rig.put(rounds(8)), turn_index=8), attempt=3)

    assert out.transcript_ref.key == f"tasks/{SPEC.task_id}/turns/0008/transcript-compact-3.json"


async def test_a_forced_view_has_a_key_of_its_own() -> None:
    # A forced compaction can come before the model call of a turn, and a proactive one after
    # it. Both are on the same turn, and the second must not overwrite the first.
    rig = Rig(notes_reply())

    out = await rig.run(compact_input(await rig.put(rounds(8)), turn_index=8, forced=True))

    assert out.transcript_ref.key == f"tasks/{SPEC.task_id}/turns/0008/transcript-forced-1.json"


async def test_the_input_view_is_not_changed() -> None:
    rig = Rig(notes_reply())
    ref = await rig.put(rounds(8))

    await rig.run(compact_input(ref))

    assert await rig.view(ref) == rounds(8)


# ---------- hints ----------
async def test_a_hint_in_the_turns_that_go_is_kept() -> None:
    # show_hints adds a hint again when its text is not in the transcript. A compaction that
    # folded the text away would make every hint come back after it.
    hint = TextBlock(text="Hint from alice: use pytest -x")
    messages = rounds(8)
    messages[4] = Message(role="user", content=[*messages[4].content, hint])  # in turn 2
    rig = Rig(notes_reply())

    out = await rig.run(compact_input(await rig.put(messages)))

    view = await rig.view(out.transcript_ref)
    assert hint in view[0].content
    assert show_hints(view, ["alice: use pytest -x"]) == view
    validate_pairs(view)


async def test_a_hint_in_the_turns_that_stay_is_not_copied() -> None:
    hint = TextBlock(text="Hint from alice: use pytest -x")
    messages = rounds(8)
    messages[-1] = Message(role="user", content=[*messages[-1].content, hint])
    rig = Rig(notes_reply())

    out = await rig.run(compact_input(await rig.put(messages)))

    view = await rig.view(out.transcript_ref)
    assert hint not in view[0].content
    assert view[-1].content[-1] == hint


async def test_a_hint_survives_a_second_compaction() -> None:
    hint = TextBlock(text="Hint from alice: use pytest -x")
    messages = rounds(8)
    messages[4] = Message(role="user", content=[*messages[4].content, hint])
    rig = Rig(notes_reply(), notes_reply())
    first = await rig.run(compact_input(await rig.put(messages)))
    view = [*await rig.view(first.transcript_ref), *rounds(12)[17:]]  # four more turns

    second = await rig.run(compact_input(await rig.put(view), notes=first.notes))

    assert hint in (await rig.view(second.transcript_ref))[0].content


async def test_a_second_round_counts_the_turns_it_folded_and_not_those_of_the_first() -> None:
    rig = Rig(notes_reply(), notes_reply())
    first = await rig.run(compact_input(await rig.put(rounds(8))))
    view = [*await rig.view(first.transcript_ref), *rounds(12)[17:]]  # four more turns

    second = await rig.run(compact_input(await rig.put(view), notes=first.notes))

    assert first.folded_turns == 5
    assert second.folded_turns == 4  # seven turns were left, and three stay
    assert second.notes is not None
    assert second.notes.covers_turns_up_to == 9  # and the notes cover all nine that are folded


def test_a_policy_that_keeps_no_turn_is_refused() -> None:
    policy = POLICY.model_copy(update={"keep_recent_turns": 0})

    with pytest.raises(ValueError, match="keep_recent_turns"):
        ContextActivities(FakeProvider(), MemoryBlobStore(), TOOLS, config_for, policy)


def test_the_activity_list_is_the_compact_activity() -> None:
    activities = ContextActivities(FakeProvider(), MemoryBlobStore(), TOOLS, config_for, POLICY)

    assert [fn.__name__ for fn in activities.activities] == ["compact"]


# ---------- the modes (LH-406) ----------
def with_mode(mode: ContextMode) -> Context:
    return POLICY.model_copy(update={"mode": mode})


@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize("mode", [ContextMode.NO_COMPACTION, ContextMode.NONE])
async def test_a_mode_without_compaction_leaves_the_view_alone(
    mode: ContextMode, forced: bool
) -> None:
    # The workflow asks for a forced compaction after an overflow. In these modes the answer is
    # that nothing changed, and the workflow then ends the run instead of asking again.
    rig = Rig(notes_reply(), policy=with_mode(mode))
    ref = await rig.put(rounds(8))

    out = await rig.run(compact_input(ref, forced=forced))

    assert out.status == "unchanged"
    assert out.folded_turns == 0
    assert out.transcript_ref == ref
    assert out.notes is None
    assert out.cost_usd == 0
    assert rig.provider.calls == []  # the compactor was not called
    assert rig.ledger.entries == []


async def test_the_full_mode_compacts() -> None:
    rig = Rig(notes_reply(), policy=with_mode(ContextMode.FULL))

    out = await rig.run(compact_input(await rig.put(rounds(8))))

    assert out.status == "ok"
    assert out.folded_turns > 0


@pytest.mark.parametrize(
    ("config", "asked", "compacts"),
    [
        (ContextMode.NONE, ContextMode.FULL, True),
        (ContextMode.NO_COMPACTION, ContextMode.FULL, True),
        (ContextMode.FULL, ContextMode.NONE, False),
        (ContextMode.FULL, ContextMode.NO_COMPACTION, False),
    ],
)
async def test_the_mode_of_the_run_wins_over_the_config(
    config: ContextMode, asked: ContextMode, compacts: bool
) -> None:
    rig = Rig(notes_reply(), policy=with_mode(config))

    out = await rig.run(compact_input(await rig.put(rounds(8)), context_mode=asked))

    assert (out.folded_turns > 0) is compacts
    assert len(rig.provider.calls) == (1 if compacts else 0)
