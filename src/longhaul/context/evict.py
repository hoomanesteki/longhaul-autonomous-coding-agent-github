"""Tier 1 of the context strategy: old tool outputs become one-line stubs (LH-404).

A tool result is read by the model on the turn after the call, and used a few turns later. After
that it is mostly dead weight, and it is paid for again on every later request. So a result that
is older than `context.elide_tool_outputs_after_turns` turns is replaced by

    [elided: <tool> output, <n> lines, handle <h>]

The handle is the one tool_exec stored the full text under (tools/output.py), so the model can
read the output again with read_output when it needs it. The results of the most recent turns
stay whole: "keep_turns" of them, counting the turn just made as the first.

The function is deterministic and cheap, and it needs no model call. The stub depends only on the
tool name, the text of the result and the call id, so the same result gives the same bytes at
every later request. That is what the prompt cache needs: it holds the prefix of the request, and
a stub that changed between two calls would drop the cache from that message on. One result ages
out per turn, so each turn changes the request at one place, K turns back, and nowhere before it.

Age is counted in the assistant messages that follow a result, so it does not depend on the turn
counter. After a compaction the first message holds the notes and the folded turns are gone, and
only the turns still in the transcript count. Running the function again on its own output
changes nothing, which is how llm_turn uses it: the stored view is already elided up to the last
turn, and each call elides one result more.

What is left alone:

- results of control tools such as `finish`. They are not run, so no output is stored for them
  and a handle would name nothing;
- results that are not longer than their stub. "edited a.py" costs less than the stub for it, and
  results such as "not run: ..." have no stored output either;
- results that already are a stub.

The module is pure. It imports the contracts, the truncation helpers and two small tool modules
for the control tool names and the handle format.
"""

from __future__ import annotations

from longhaul.context.truncate import marker_line_count, split_lines
from longhaul.contracts import Block, Message, ToolResultBlock, ToolUseBlock
from longhaul.tools.control import CONTROL_TOOL_NAMES
from longhaul.tools.output import output_handle

__all__ = ["ELIDED_PREFIX", "elide_old_outputs", "elision_stub"]

ELIDED_PREFIX = "[elided: "
UNKNOWN_TOOL = "tool"


def elision_stub(tool: str, lines: int, handle: str) -> str:
    """The text that stands in for the output of `tool`, which had `lines` lines."""
    unit = "line" if lines == 1 else "lines"
    return f"{ELIDED_PREFIX}{tool} output, {lines} {unit}, handle {handle}]"


def _stub(block: ToolResultBlock, tool: str) -> ToolResultBlock:
    """The result with its text replaced by a stub, or as it is when a stub would not help."""
    if tool in CONTROL_TOOL_NAMES or block.content.startswith(ELIDED_PREFIX):
        return block
    # A cut output carries the lines of the whole in its marker. The text itself has 121 at most.
    cut = marker_line_count(block.content)
    lines = cut if cut is not None else len(split_lines(block.content))
    stub = elision_stub(tool, lines, output_handle(block.tool_use_id))
    if len(stub) >= len(block.content):
        return block
    return block.model_copy(update={"content": stub})


def _elide(message: Message, tools: dict[str, str]) -> Message:
    blocks: list[Block] = [
        _stub(block, tools.get(block.tool_use_id, UNKNOWN_TOOL))
        if isinstance(block, ToolResultBlock)
        else block
        for block in message.content
    ]
    if blocks == message.content:
        return message
    return Message(role=message.role, content=blocks)


def elide_old_outputs(messages: list[Message], keep_turns: int) -> list[Message]:
    """`messages` with the results of all but the last `keep_turns` turns replaced by stubs.

    A turn is an assistant message with the user message that answers its tool calls. The
    messages that need no change are returned as they are, and the argument is not changed.
    """
    if keep_turns < 1:
        raise ValueError(f"keep_turns has to be at least 1, got {keep_turns}")
    turns = sum(1 for message in messages if message.role == "assistant")
    tools: dict[str, str] = {}
    seen = 0  # assistant messages up to the message being looked at
    out: list[Message] = []
    for message in messages:
        if message.role == "assistant":
            seen += 1
            tools.update({b.id: b.name for b in message.content if isinstance(b, ToolUseBlock)})
            out.append(message)
        elif turns - seen >= keep_turns:
            out.append(_elide(message, tools))
        else:
            out.append(message)
    return out
