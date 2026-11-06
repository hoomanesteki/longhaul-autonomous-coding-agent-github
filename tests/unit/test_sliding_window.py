"""The sliding window of the naive baseline (LH-903): drop the oldest whole turns at the limit.

`window` cuts a transcript and `fit` finds the longest cut that fits. Both are pure, so the
tests build transcripts by hand and with hypothesis and check the rules the Messages API
enforces with a 400 that is never retried: a tool_use is never left without its result and a
result never without its tool_use. llm_turn uses these two functions, and
test_activities_llm_context.py checks that it sends nothing over the hard limit.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from longhaul.agent.turns import validate_pairs
from longhaul.context.sliding import fit, window
from longhaul.contracts import (
    Block,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.providers.base import format_turn_offset, read_turn_offset, turn_index

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

TASK = "Work through the task in the system prompt."


def text(body: str) -> TextBlock:
    return TextBlock(text=body)


def use(call_id: str) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name="read_file", input={"path": f"{call_id}.py"})


def res(call_id: str, body: str = "output") -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=call_id, content=body)


def user(*blocks: Block) -> Message:
    return Message(role="user", content=list(blocks))


def assistant(*blocks: Block) -> Message:
    return Message(role="assistant", content=list(blocks))


def rounds(count: int, body: str = "output") -> list[Message]:
    """The task, then `count` rounds of one call and its result."""
    messages = [user(text(TASK))]
    for n in range(count):
        messages += [assistant(use(f"c{n}")), user(res(f"c{n}", f"{body} {n}"))]
    return messages


def assistants(messages: list[Message]) -> int:
    return sum(1 for m in messages if m.role == "assistant")


def size(messages: list[Message]) -> int:
    """A stand-in for the token count: characters of text, calls and results."""
    total = 0
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                total += len(block.text)
            elif isinstance(block, ToolResultBlock):
                total += len(block.content)
            elif isinstance(block, ToolUseBlock):
                total += len(block.id) + len(block.name) + len(str(block.input))
    return total


# ---------- window ----------
def test_a_window_keeps_the_task_and_the_newest_turns() -> None:
    messages = rounds(6)

    slid = window(messages, 2)

    assert assistants(slid) == 2
    assert slid[1:] == messages[-4:]  # the last two rounds, whole
    assert slid[0].role == "user"
    assert slid[0].content[0] == text(TASK)


def test_a_window_does_not_change_the_list_it_is_given() -> None:
    messages = rounds(6)
    before = [m.model_copy(deep=True) for m in messages]

    window(messages, 2)

    assert messages == before


def test_a_window_that_keeps_every_turn_returns_the_transcript_as_it_is() -> None:
    messages = rounds(3)
    assert window(messages, 3) is messages
    assert window(messages, 9) is messages


def test_a_transcript_with_no_turn_has_nothing_to_drop() -> None:
    messages = rounds(0)
    assert window(messages, 1) is messages


def test_a_window_keeps_the_turn_count_so_a_script_or_a_log_still_knows_the_turn() -> None:
    messages = rounds(6)

    slid = window(messages, 2)

    assert read_turn_offset(slid) == 4
    assert turn_index(slid) == turn_index(messages) == 6
    assert format_turn_offset(4) in [b.text for b in slid[0].content if isinstance(b, TextBlock)]


def test_a_window_of_a_window_adds_up_and_leaves_one_marker() -> None:
    once = window(rounds(8), 5)
    twice = window(once, 2)

    assert turn_index(twice) == 8
    markers = [b for b in twice[0].content if isinstance(b, TextBlock) and "turn_offset" in b.text]
    assert len(markers) == 1
    assert twice[0].content[0] == text(TASK)


def test_a_tool_round_with_parallel_calls_is_dropped_whole_or_kept_whole() -> None:
    messages = [
        user(text(TASK)),
        assistant(use("a1"), use("a2"), use("a3")),
        user(res("a1"), res("a2"), res("a3")),
        assistant(use("b1"), use("b2")),
        user(res("b1"), res("b2")),
        assistant(use("c1")),
        user(res("c1")),
    ]

    for keep in (1, 2):
        slid = window(messages, keep)
        validate_pairs(slid)
        ids = {b.id for m in slid for b in m.content if isinstance(b, ToolUseBlock)}
        answered = {
            b.tool_use_id for m in slid for b in m.content if isinstance(b, ToolResultBlock)
        }
        assert ids == answered

    assert assistants(window(messages, 2)) == 2
    assert assistants(window(messages, 1)) == 1


@st.composite
def transcripts(draw: st.DrawFn) -> list[Message]:
    """A transcript as the loop builds it: rounds of up to three parallel calls and a hint."""
    messages = [user(text(TASK))]
    ids = iter(range(10_000))
    for _ in range(draw(st.integers(min_value=0, max_value=16))):
        calls = [f"toolu_{next(ids)}" for _ in range(draw(st.integers(0, 3)))]
        blocks: list[Block] = [text("thinking aloud")] if not calls or draw(st.booleans()) else []
        messages.append(assistant(*blocks, *(use(c) for c in calls)))
        if calls:
            answers: list[Block] = [res(c, "x" * draw(st.integers(0, 40))) for c in calls]
            if draw(st.booleans()):
                answers.append(text("Hint from @sam: try the other file"))
            messages.append(user(*answers))
        else:
            messages.append(user(text("Use tools or call finish.")))
    return messages


PROPERTY = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@PROPERTY
@given(transcripts(), st.integers(min_value=1, max_value=20))
def test_a_window_never_orphans_a_call_or_a_result(messages: list[Message], keep: int) -> None:
    slid = window(messages, keep)

    validate_pairs(slid)
    assert slid[0].content[0] == messages[0].content[0]  # the task is never dropped
    assert turn_index(slid) == turn_index(messages)
    if assistants(messages) <= keep:
        assert slid is messages
    else:
        assert assistants(slid) >= keep
        # Either a cut was found and turns went, or no cut point was left and nothing changed.
        assert slid is messages or assistants(slid) < assistants(messages)


# ---------- fit ----------
async def measure_by_size(view: list[Message]) -> int:
    return size(view)


def over(limit: int) -> Callable[[int], bool]:
    """The test of the caller: a request is over when it takes `limit` or more."""
    return lambda tokens: tokens >= limit


async def test_a_transcript_under_the_limit_is_returned_as_it_is() -> None:
    messages = rounds(5)

    view, tokens = await fit(messages, measure_by_size, lambda t: t >= size(messages) + 1)

    assert view is messages
    assert tokens == size(messages)


async def test_the_oldest_turns_go_first_and_only_as_many_as_it_takes() -> None:
    messages = rounds(10)
    # Room for the task and the newest four rounds, by the measure.
    room = size(window(messages, 4)) + 1

    view, tokens = await fit(messages, measure_by_size, over(room))

    assert assistants(view) == 4
    assert view[1:] == messages[-8:]
    assert tokens == size(view) < room
    validate_pairs(view)


async def test_when_no_cut_fits_the_smallest_one_comes_back_over_the_limit() -> None:
    messages = rounds(6)
    room = size(window(messages, 1))  # not even one round fits: the limit is not reached

    view, tokens = await fit(messages, measure_by_size, over(room))

    assert assistants(view) == 1
    assert tokens >= room  # the caller sees that it is over, and refuses the request


async def test_a_transcript_with_one_turn_cannot_be_cut_and_comes_back_over() -> None:
    messages = rounds(1)

    view, tokens = await fit(messages, measure_by_size, over(size(messages)))

    assert view is messages
    assert tokens == size(messages)


async def test_a_measure_that_counts_what_the_request_adds_is_honoured() -> None:
    messages = rounds(10)
    extra = 200  # the system prompt and the tools: the same for every cut

    async def measure(view: list[Message]) -> int:
        return size(view) + extra

    room = size(window(messages, 3)) + extra + 1

    view, tokens = await fit(messages, measure, over(room))

    assert assistants(view) == 3
    assert tokens == size(view) + extra


async def test_the_search_asks_for_few_measurements_even_for_a_long_transcript() -> None:
    messages = rounds(200)
    asked = 0

    async def measure(view: list[Message]) -> int:
        nonlocal asked
        asked += 1
        return size(view)

    room = size(window(messages, 37)) + 1

    view, _ = await fit(messages, measure, over(room))

    assert assistants(view) == 37
    # One look at the whole transcript, then a binary search over the 199 cuts.
    assert asked <= 1 + math.ceil(math.log2(200)) + 1


@PROPERTY
@given(transcripts(), st.integers(min_value=0, max_value=2_000))
def test_fit_returns_the_longest_window_that_fits(messages: list[Message], room: int) -> None:
    # hypothesis does not await a test, so the coroutine runs here.
    view, tokens = asyncio.run(fit(messages, measure_by_size, over(room)))

    validate_pairs(view)
    assert tokens == size(view)
    assert turn_index(view) == turn_index(messages)
    fits = [window(messages, keep) for keep in range(1, max(assistants(messages), 1) + 1)]
    fitting = [v for v in fits if size(v) < room]
    if size(messages) < room:
        assert view is messages
    elif fitting:
        # The longest one that fits: no longer window fits, and this one does.
        assert size(view) < room
        assert assistants(view) == max(assistants(v) for v in fitting)
    else:
        assert size(view) >= room  # nothing fits, so the caller is told
