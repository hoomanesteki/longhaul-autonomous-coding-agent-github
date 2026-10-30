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
