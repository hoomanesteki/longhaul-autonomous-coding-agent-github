"""Convert contract messages to and from the Anthropic Messages API shape.

Kept out of contracts so that module stays free of vendor details. Blocks are
plain dicts, so the anthropic SDK is not needed here.

to_anthropic leaves out what the API answers with a 400: a text block with no
text, and a message with no blocks. A transcript can hold both, for example
after the documented empty response, and a 400 is not retried, so the task
would end. Where a skipped message leaves two turns of one role side by side,
the API reads them as one turn.
"""

from typing import Any, Literal

from longhaul.contracts import Block, Message, TextBlock, ToolResultBlock, ToolUseBlock


def _block_to_dict(block: Block) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        # Only a block with nothing in it goes. Text with spaces around it is sent as written.
        return {"type": "text", "text": block.text} if block.text.strip() else None
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    return {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
        "is_error": block.is_error,
    }


def _block_from_dict(raw: dict[str, Any]) -> Block:
    kind = raw.get("type")
    if kind == "text":
        return TextBlock(text=raw["text"])
    if kind == "tool_use":
        return ToolUseBlock(id=raw["id"], name=raw["name"], input=raw["input"])
    if kind == "tool_result":
        return ToolResultBlock(
            tool_use_id=raw["tool_use_id"],
            content=raw["content"],
            is_error=raw.get("is_error", False),
        )
    raise ValueError(f"unsupported block type: {kind!r}")


def to_anthropic(messages: list[Message]) -> list[dict[str, Any]]:
    wire: list[dict[str, Any]] = []
    for message in messages:
        blocks = [d for d in map(_block_to_dict, message.content) if d is not None]
        if blocks:
            wire.append({"role": message.role, "content": blocks})
    return wire


def from_anthropic(blocks: list[dict[str, Any]], role: Literal["user", "assistant"]) -> Message:
    return Message(role=role, content=[_block_from_dict(b) for b in blocks])
