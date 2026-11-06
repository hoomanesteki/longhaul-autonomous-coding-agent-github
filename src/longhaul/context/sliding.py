"""The sliding window: how the naive baseline stays under the hard limit (LH-903).

A loop with no context engineering usually keeps the window from overflowing in the simplest
way there is: when the next request would be too big, it throws away the oldest turns. The
`sliding` mode (longhaul.context.modes) does exactly that and nothing else, so the evaluation
can say what elision, compaction and notes buy on top of it (plan section 8.3). It does not
summarize, it does not elide old tool outputs, and it does not tell the model what it dropped
beyond the turn counter below.

Two steps, both without I/O.

1. `window()` cuts a transcript to its newest turns. A turn is an assistant message with the user
   message that answers its tool calls. The cut comes from context/compact.py `find_cut`, which
   only returns an index where no tool_use waits for its result on the far side and no result
   answers a call on the near side. The Messages API answers 400 to either, and a 400 is never
   retried, so a cut inside a tool round would end the task. Message 0 stays: it is the first
   user message, and the task text is in the system prompt either way.
2. `fit()` finds the longest window that is under the limit. Dropping turns only makes a request
   smaller, so it searches the number of turns to keep by halving, which takes a few
   measurements even for a transcript of 150 turns. The measure is a callback, because the
   caller knows what else is in the request (the system prompt, the tools, the text of a paused
   reply) and whether an exact count from the provider is worth its round trip.

The turn counter. A window removes assistant messages, but the task is still on the same turn,
and the log, the scripted provider and `turn_index()` all count turns. So `window()` writes the
number of turns dropped so far on a line of its own in the first message, in the format that
providers.base documents for compaction (format_turn_offset). A window of a window replaces the
earlier line instead of adding a second one.

What a window costs. It changes the start of the request, so the provider's prompt cache is
read up to the first message only, and every turn after the limit pays to write the conversation
again. That is part of what the baseline measures.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from longhaul.agent.turns import validate_pairs
from longhaul.context.compact import find_cut
from longhaul.contracts import Block, Message, TextBlock
from longhaul.providers.base import TURN_OFFSET_FORMAT, format_turn_offset, turn_index

__all__ = ["fit", "window"]

_HEAD, _TAIL = TURN_OFFSET_FORMAT.split("{offset}")
# A block that holds the marker and nothing else, which is how window() writes it.
_MARKER_BLOCK = re.compile(rf"\s*{re.escape(_HEAD)}\d+{re.escape(_TAIL)}\s*")


def _is_marker(block: Block) -> bool:
    return isinstance(block, TextBlock) and _MARKER_BLOCK.fullmatch(block.text) is not None


def window(messages: list[Message], keep_turns: int) -> list[Message]:
    """The transcript cut to its newest turns, or `messages` itself when no cut is possible.

    At least `keep_turns` turns are kept: a cut may keep more, when the turn that would start
    the kept part is in the middle of a tool round. The first message is always kept. Nothing is
    cut from a transcript that has `keep_turns` turns or fewer, or none that folds a turn.
    `messages` is not changed.
    """
    cut = find_cut(messages, keep_turns)
    if cut == 0:
        return messages
    first = messages[0]
    kept = [block for block in first.content if not _is_marker(block)]
    marker = TextBlock(text=format_turn_offset(turn_index(messages[:cut])))
    slid = [Message(role=first.role, content=[*kept, marker]), *messages[cut:]]
    validate_pairs(slid)
    return slid


async def fit(
    messages: list[Message],
    measure: Callable[[list[Message]], Awaitable[int]],
    over: Callable[[int], bool],
) -> tuple[list[Message], int]:
    """The longest window that is not over the limit, with its measure.

    `measure(view)` is the size of the request that `view` would make, and `over(size)` says
    whether that size is past the limit. A transcript that is not over is returned as it is. When
    no window gets under the limit, the shortest one comes back with its size, which is over, so
    the caller can refuse the request: a single result larger than the window cannot be fixed by
    dropping what came before it.
    """
    whole = await measure(messages)
    if not over(whole):
        return messages, whole
    turns = sum(1 for message in messages if message.role == "assistant")
    best: tuple[list[Message], int] | None = None
    shortest = (messages, whole)
    low, high = 1, turns - 1  # the number of turns to keep
    while low <= high:
        keep = (low + high) // 2
        view = window(messages, keep)
        size = await measure(view)
        if over(size):
            shortest = (view, size)
            high = keep - 1
        else:
            best = (view, size)
            low = keep + 1
    return best if best is not None else shortest
