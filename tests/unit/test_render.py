from typing import Any

import pytest

from longhaul.agent.render import from_anthropic, to_anthropic
from longhaul.contracts import Message, TextBlock, ToolResultBlock, ToolUseBlock

pytestmark = [pytest.mark.unit, pytest.mark.phase1]


def test_to_anthropic_shapes() -> None:
    msgs = [
        Message(
            role="assistant",
            content=[TextBlock(text="hi"), ToolUseBlock(id="c1", name="run", input={"a": 1})],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="c1", content="ok")]),
    ]
    assert to_anthropic(msgs) == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "c1", "name": "run", "input": {"a": 1}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "ok", "is_error": False}
            ],
        },
    ]


def test_round_trip() -> None:
    msg = Message(
        role="assistant",
        content=[
            TextBlock(text="hi"),
            ToolUseBlock(id="c1", name="run", input={"a": [1, 2]}),
            ToolResultBlock(tool_use_id="c1", content="bad", is_error=True),
        ],
    )
    [wire] = to_anthropic([msg])
    assert from_anthropic(wire["content"], "assistant") == msg


def test_from_anthropic_defaults_and_unknown() -> None:
    msg = from_anthropic([{"type": "tool_result", "tool_use_id": "c1", "content": "x"}], "user")
    assert msg.content == [ToolResultBlock(tool_use_id="c1", content="x", is_error=False)]
    with pytest.raises(ValueError):
        from_anthropic([{"type": "image"}], "user")


# ---------- what the Messages API refuses ----------
# The API answers 400 for a text block with no text and for a message with no content
# (the last assistant message excepted). Both would end a task with a non-retryable error.
TOOL_USE = ToolUseBlock(id="c1", name="run", input={})
TOOL_USE_WIRE = {"type": "tool_use", "id": "c1", "name": "run", "input": {}}


def _roles(wire: list[dict[str, Any]]) -> list[str]:
    return [m["role"] for m in wire]


def test_an_empty_text_block_next_to_a_tool_use_is_not_sent() -> None:
    msg = Message(role="assistant", content=[TextBlock(text=""), TOOL_USE])

    assert to_anthropic([msg]) == [{"role": "assistant", "content": [TOOL_USE_WIRE]}]


@pytest.mark.parametrize("text", ["", " ", "\n", "\t", " \r\n\t "])
def test_blank_text_blocks_are_dropped(text: str) -> None:
    msg = Message(role="user", content=[TextBlock(text=text), TextBlock(text="go")])

    assert to_anthropic([msg]) == [{"role": "user", "content": [{"type": "text", "text": "go"}]}]


def test_text_is_sent_as_written() -> None:
    # Only a block with nothing in it is dropped. Spaces around real text can matter.
    msg = Message(role="user", content=[TextBlock(text="  indented\n")])

    assert to_anthropic([msg]) == [
        {"role": "user", "content": [{"type": "text", "text": "  indented\n"}]}
    ]


@pytest.mark.parametrize(
    "content",
    [[], [TextBlock(text="")], [TextBlock(text=" \n"), TextBlock(text="")]],
    ids=["no-blocks", "one-empty-text", "only-blank-text"],
)
def test_a_message_with_nothing_to_send_is_skipped(content: list[TextBlock]) -> None:
    go = Message(role="user", content=[TextBlock(text="go")])
    empty = Message(role="assistant", content=list(content))
    more = Message(role="user", content=[TextBlock(text="more")])

    wire = to_anthropic([go, empty, more])

    assert _roles(wire) == ["user", "user"]
    assert [m["content"][0]["text"] for m in wire] == ["go", "more"]


def test_an_empty_final_assistant_message_is_skipped() -> None:
    go = Message(role="user", content=[TextBlock(text="go")])

    assert _roles(to_anthropic([go, Message(role="assistant", content=[])])) == ["user"]


def test_an_empty_reply_after_a_tool_result_does_not_reach_the_api() -> None:
    # The documented empty response: end_turn with no content, often after a tool result.
    transcript = [
        Message(role="user", content=[TextBlock(text="fix it")]),
        Message(role="assistant", content=[TOOL_USE]),
        Message(role="user", content=[ToolResultBlock(tool_use_id="c1", content="ok")]),
        Message(role="assistant", content=[]),
        Message(role="user", content=[TextBlock(text="Use tools or call finish")]),
    ]

    wire = to_anthropic(transcript)

    assert _roles(wire) == ["user", "assistant", "user", "user"]
    assert all(m["content"] for m in wire)
    assert wire[-1]["content"] == [{"type": "text", "text": "Use tools or call finish"}]


def test_an_empty_tool_result_is_kept() -> None:
    # Dropping it would leave the tool_use before it without an answer, which is also a 400.
    results = [ToolResultBlock(tool_use_id="c1", content="")]

    wire = to_anthropic([Message(role="user", content=list(results))])

    assert wire == [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "", "is_error": False}
            ],
        }
    ]


def test_nothing_to_render() -> None:
    assert to_anthropic([]) == []
