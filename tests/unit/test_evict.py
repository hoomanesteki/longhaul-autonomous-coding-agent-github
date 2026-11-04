"""Tier 1 of the context strategy: old tool outputs become one-line stubs (LH-404).

The stub has to be the same bytes every time it is made. The prompt cache holds the prefix of
the request, and a stub that changed from one call to the next would throw the prefix away.
"""

import pytest
from longhaul.context.evict import ELIDED_PREFIX, elide_old_outputs, elision_stub

from longhaul.agent.turns import validate_pairs
from longhaul.context.truncate import shape_output
from longhaul.contracts import (
    Message,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.tools.output import output_handle

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

FIRST = Message(role="user", content=[TextBlock(text="Start on the task.")])


def output(lines: int, tag: str = "") -> str:
    return "\n".join(f"{tag}line {n}: some output that takes room" for n in range(1, lines + 1))


def turn(n: int, *, tool: str = "read_file", lines: int = 30, error: bool = False) -> list[Message]:
    """One assistant message with one call, and the user message that answers it."""
    call_id = f"call-{n}"
    return [
        Message(role="assistant", content=[ToolUseBlock(id=call_id, name=tool, input={"n": n})]),
        Message(
            role="user",
            content=[
                ToolResultBlock(
                    tool_use_id=call_id, content=output(lines, f"t{n} "), is_error=error
                )
            ],
        ),
    ]


def transcript(turns: int, **kwargs: object) -> list[Message]:
    messages = [FIRST]
    for n in range(1, turns + 1):
        messages += turn(n, **kwargs)  # type: ignore[arg-type]
    return messages


def results(messages: list[Message]) -> list[ToolResultBlock]:
    return [b for m in messages for b in m.content if isinstance(b, ToolResultBlock)]


def test_results_of_the_last_turns_stay_whole_and_older_ones_become_stubs() -> None:
    messages = transcript(6)

    elided = elide_old_outputs(messages, keep_turns=2)

    kept = results(elided)
    assert [r.content.startswith(ELIDED_PREFIX) for r in kept] == [True] * 4 + [False] * 2
    assert kept[-2:] == results(messages)[-2:]


def test_a_stub_names_the_tool_the_lines_and_the_handle() -> None:
    elided = elide_old_outputs(transcript(4, tool="run_tests", lines=30), keep_turns=1)

    first = results(elided)[0]
    assert first.content == "[elided: run_tests output, 30 lines, handle out_call-1]"
    assert first.content == elision_stub("run_tests", 30, output_handle("call-1"))


def test_a_stub_for_one_line_says_line() -> None:
    assert (
        elision_stub("read_file", 1, "out_x") == "[elided: read_file output, 1 line, handle out_x]"
    )


def test_nothing_is_elided_while_the_transcript_has_no_more_turns_than_it_keeps() -> None:
    messages = transcript(3)

    assert elide_old_outputs(messages, keep_turns=3) == messages
    assert elide_old_outputs(messages, keep_turns=5) == messages


def test_the_result_of_the_turn_just_made_is_never_elided() -> None:
    messages = transcript(5)

    elided = elide_old_outputs(messages, keep_turns=1)

    assert not results(elided)[-1].content.startswith(ELIDED_PREFIX)


def test_a_stub_is_the_same_bytes_at_every_later_call() -> None:
    # The stub of turn 1 made when turn 4 ends, and again when turn 9 ends, must match.
    early = elide_old_outputs(transcript(4), keep_turns=2)
    late = elide_old_outputs(transcript(9), keep_turns=2)

    assert results(early)[0] == results(late)[0]
    assert results(early)[1] == results(late)[1]


def test_elision_is_idempotent() -> None:
    once = elide_old_outputs(transcript(8), keep_turns=3)

    assert elide_old_outputs(once, keep_turns=3) == once


def test_elision_can_follow_a_view_that_was_elided_a_turn_earlier() -> None:
    # What llm_turn does: the stored view of the last turn is elided again with one more turn.
    step = elide_old_outputs(transcript(5), keep_turns=2)
    step = [*step, *turn(6)]

    assert elide_old_outputs(step, keep_turns=2) == elide_old_outputs(transcript(6), keep_turns=2)


def test_the_input_is_not_changed() -> None:
    messages = transcript(6)
    before = [m.model_copy(deep=True) for m in messages]

    elide_old_outputs(messages, keep_turns=2)

    assert messages == before


def test_the_error_flag_and_the_pairing_survive() -> None:
    messages = [FIRST, *turn(1, error=True), *turn(2), *turn(3), *turn(4)]

    elided = elide_old_outputs(messages, keep_turns=2)

    first = results(elided)[0]
    assert first.is_error is True
    assert first.content.startswith(ELIDED_PREFIX)
    validate_pairs(elided)


def test_a_result_shorter_than_its_stub_is_left_alone() -> None:
    short = Message(
        role="user", content=[ToolResultBlock(tool_use_id="call-1", content="edited a.py")]
    )
    messages = [
        FIRST,
        Message(role="assistant", content=[ToolUseBlock(id="call-1", name="edit_file", input={})]),
        short,
        *turn(2),
        *turn(3),
    ]

    assert elide_old_outputs(messages, keep_turns=1)[2] == short


def test_a_control_tool_result_is_left_alone() -> None:
    # finish has no stored output, so a stub would name a handle that does not exist.
    long_answer = "finish needs a summary: " + "say what you changed. " * 10
    messages = [
        FIRST,
        Message(role="assistant", content=[ToolUseBlock(id="call-1", name="finish", input={})]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="call-1", content=long_answer)]),
        *turn(2),
        *turn(3),
    ]

    assert elide_old_outputs(messages, keep_turns=1)[2].content[0] == ToolResultBlock(
        tool_use_id="call-1", content=long_answer
    )


def test_a_shaped_output_reports_the_lines_of_the_whole_output() -> None:
    full = output(10_000)
    shaped = shape_output(full, 200, output_handle("call-1"))
    messages = [
        FIRST,
        Message(role="assistant", content=[ToolUseBlock(id="call-1", name="run_tests", input={})]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="call-1", content=shaped)]),
        *turn(2),
        *turn(3),
    ]

    stub = results(elide_old_outputs(messages, keep_turns=1))[0].content

    assert stub == "[elided: run_tests output, 10000 lines, handle out_call-1]"


def test_several_results_in_one_message_each_get_their_own_stub() -> None:
    messages = [
        FIRST,
        Message(
            role="assistant",
            content=[
                ToolUseBlock(id="a", name="read_file", input={}),
                ToolUseBlock(id="b", name="search", input={}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="a", content=output(20)),
                ToolResultBlock(tool_use_id="b", content=output(40)),
            ],
        ),
        *turn(2),
        *turn(3),
    ]

    old = elide_old_outputs(messages, keep_turns=1)[2].content

    assert [b.content for b in old if isinstance(b, ToolResultBlock)] == [
        "[elided: read_file output, 20 lines, handle out_a]",
        "[elided: search output, 40 lines, handle out_b]",
    ]


def test_text_in_a_user_message_is_never_touched() -> None:
    hint = Message(role="user", content=[TextBlock(text="Hint from alice: use pytest -x")])
    messages = [FIRST, *turn(1), hint, *turn(2), *turn(3), *turn(4)]

    elided = elide_old_outputs(messages, keep_turns=1)

    assert hint in elided


def test_age_counts_the_assistant_messages_that_are_left_after_a_compaction() -> None:
    # After a compaction the first message holds the notes and the turn marker. Only the turns
    # that are still in the transcript count.
    notes = Message(
        role="user",
        content=[
            TextBlock(text="Start."),
            TextBlock(text="## Progress notes\n\n[longhaul:turn_offset=9]"),
        ],
    )
    messages = [notes, *turn(10), *turn(11), *turn(12)]

    kept = results(elide_old_outputs(messages, keep_turns=2))

    assert [r.content.startswith(ELIDED_PREFIX) for r in kept] == [True, False, False]


def test_keep_turns_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="keep_turns"):
        elide_old_outputs(transcript(3), keep_turns=0)


def test_a_transcript_with_nothing_to_elide_comes_back_equal() -> None:
    assert elide_old_outputs([], keep_turns=2) == []
    assert elide_old_outputs([FIRST], keep_turns=2) == [FIRST]
