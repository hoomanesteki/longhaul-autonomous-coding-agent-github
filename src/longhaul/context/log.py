"""The log of a task: every message, kept apart from the view the model reads (LH-404).

A task keeps two records of its transcript. The view is what the model sees. It is elided by
tier 1 and cut down to notes by tier 2, and `CarryState.transcript_ref` points at it. The log is
the whole transcript, never elided and never compacted, and a report reads it to say what the
agent did.

Where the log lives. CarryState has one transcript ref and may not change, and the log has to
survive Continue-As-New. So it is not a blob that a ref points at. It is a set of small blobs
whose keys come from the task id and the turn alone:

    tasks/<task_id>/log/<turn:04d>/reply.json     what llm_turn added: hints, then the reply
    tasks/<task_id>/log/<turn:04d>/results.json   what append_results added: results, then a nudge

Nothing in the workflow has to carry them, and any worker can find them. A part is written once
per step and never changed afterwards, which is what makes the log append-only. It is also why a
retried activity cannot duplicate anything: it writes the same key again, with the same content,
and the log reads back the same. Growing one blob instead would cost a read and a write of the
whole log on every step, and a retry could not tell whether its append had already happened.

A part holds only what its step added, as `added_messages` works it out. A hint is text that
llm_turn added to the last user message of the view. In the log it is a message of its own that
comes before the reply, and `read_log` joins messages of the same role again, so the log reads
like the transcript the model would have seen without tiers 1 and 2.

Reading. `read_log` takes the parts in turn order and stops at the first turn that has none. A
run that ended in the middle of a turn has the reply part and no results part, and the log ends
with that reply. A part is read by its key with no ref to check it against, because nothing keeps
the refs. It is parsed as messages, and a damaged part is an error and never an early end. The
log is for people and reports. The model never reads it.
"""

from __future__ import annotations

import itertools
from typing import Literal

from pydantic import TypeAdapter

from longhaul.blobs.base import BlobNotFoundError, BlobStoreBase, blob_key
from longhaul.contracts import Message

__all__ = ["LogPart", "added_messages", "append_to_log", "log_key", "merge_adjacent", "read_log"]

LogPart = Literal["reply", "results"]
PARTS: tuple[LogPart, ...] = ("reply", "results")

_MESSAGES = TypeAdapter(list[Message])


def log_key(task_id: str, turn_index: int, part: LogPart) -> str:
    """tasks/<task_id>/log/<turn:04d>/<part>.json"""
    return blob_key(task_id, "log", turn_index, f"{part}.json")


def added_messages(before: list[Message], after: list[Message]) -> list[Message]:
    """What a step added to a transcript: text joined to the last message, and new messages.

    `after` has to be `before` with text blocks added to its last message and messages added at
    its end, which is all that the steps of a turn do (turns.show_hints, append_assistant,
    answer_tool_calls and append_user_text). Anything else is a ValueError, so a step that
    rewrote the past cannot be mistaken for one that appended to it.
    """
    if not before:
        return list(after)
    last = len(before) - 1
    if len(after) < len(before) or after[:last] != before[:last]:
        raise ValueError("`after` does not extend `before`: it changes or drops earlier messages")
    old, new = before[last], after[last]
    if new.role != old.role or new.content[: len(old.content)] != old.content:
        raise ValueError("`after` does not extend `before`: its last message changed")
    extra = new.content[len(old.content) :]
    joined = [Message(role=new.role, content=extra)] if extra else []
    return [*joined, *after[len(before) :]]


def merge_adjacent(messages: list[Message]) -> list[Message]:
    """The messages with each run of the same role joined into one, as the API has them."""
    merged: list[Message] = []
    for message in messages:
        if merged and merged[-1].role == message.role:
            merged[-1] = Message(role=message.role, content=[*merged[-1].content, *message.content])
        else:
            merged.append(message)
    return merged


async def append_to_log(
    blobs: BlobStoreBase, task_id: str, turn_index: int, part: LogPart, messages: list[Message]
) -> None:
    """Write one part of the log. Writing the same part again replaces it."""
    await blobs.put_json(messages, log_key(task_id, turn_index, part))


async def _read_part(
    blobs: BlobStoreBase, task_id: str, turn_index: int, part: LogPart
) -> list[Message] | None:
    try:
        data = await blobs.get_by_key(log_key(task_id, turn_index, part))
    except BlobNotFoundError:
        return None
    return _MESSAGES.validate_json(data)


async def read_log(blobs: BlobStoreBase, task_id: str) -> list[Message]:
    """The whole transcript of the task, in order, with messages of one role joined."""
    messages: list[Message] = []
    for turn_index in itertools.count():
        parts = [await _read_part(blobs, task_id, turn_index, part) for part in PARTS]
        if all(part is None for part in parts):
            break
        for part in parts:
            messages += part or []
    return merge_adjacent(messages)
