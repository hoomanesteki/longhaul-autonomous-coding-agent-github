"""The log of a task: every message, kept apart from the view the model reads (LH-404).

The log is written one part at a time under keys that come from the task id and the turn, so
it needs no state in the workflow, survives Continue-As-New, and writing a part again (a retried
activity) replaces it and adds nothing.
"""

import pytest
from longhaul.context.log import added_messages, append_to_log, log_key, read_log

from longhaul.agent.turns import validate_pairs
from longhaul.blobs.base import BlobNotFoundError
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import Message, TextBlock, ToolResultBlock, ToolUseBlock

pytestmark = [pytest.mark.unit, pytest.mark.phase4]

TASK = "lh-acme-calc-42"


def user(*texts: str) -> Message:
    return Message(role="user", content=[TextBlock(text=t) for t in texts])


def assistant(call_id: str) -> Message:
    return Message(role="assistant", content=[ToolUseBlock(id=call_id, name="read_file", input={})])


def answer(call_id: str, text: str = "ok") -> Message:
    return Message(role="user", content=[ToolResultBlock(tool_use_id=call_id, content=text)])


def test_a_part_has_a_key_made_from_the_task_the_turn_and_the_part() -> None:
    assert log_key(TASK, 3, "reply") == f"tasks/{TASK}/log/0003/reply.json"
    assert log_key(TASK, 12, "results") == f"tasks/{TASK}/log/0012/results.json"


def test_a_task_id_that_is_not_a_key_part_is_refused() -> None:
    with pytest.raises(ValueError, match="task_id"):
        log_key("../other", 0, "reply")


async def test_an_empty_log_reads_as_no_messages() -> None:
    assert await read_log(MemoryBlobStore(), TASK) == []


async def test_the_parts_read_back_in_turn_order() -> None:
    blobs = MemoryBlobStore()
    await append_to_log(blobs, TASK, 0, "reply", [user("start"), assistant("c0")])
    await append_to_log(blobs, TASK, 0, "results", [answer("c0", "first")])
    await append_to_log(blobs, TASK, 1, "reply", [assistant("c1")])
    await append_to_log(blobs, TASK, 1, "results", [answer("c1", "second")])

    log = await read_log(blobs, TASK)

    assert log == [
        user("start"),
        assistant("c0"),
        answer("c0", "first"),
        assistant("c1"),
        answer("c1", "second"),
    ]
    validate_pairs(log)


async def test_a_part_written_in_any_order_still_reads_in_turn_order() -> None:
    blobs = MemoryBlobStore()
    await append_to_log(blobs, TASK, 1, "reply", [assistant("c1")])
    await append_to_log(blobs, TASK, 0, "reply", [user("start"), assistant("c0")])
    await append_to_log(blobs, TASK, 0, "results", [answer("c0")])
    await append_to_log(blobs, TASK, 1, "results", [answer("c1")])

    log = await read_log(blobs, TASK)

    assert [m.role for m in log] == ["user", "assistant", "user", "assistant", "user"]


async def test_writing_a_part_again_replaces_it() -> None:
    # A retried activity writes the part it wrote before. The log must not hold it twice.
    blobs = MemoryBlobStore()
    await append_to_log(blobs, TASK, 0, "reply", [user("start"), assistant("c0")])
    await append_to_log(blobs, TASK, 0, "reply", [user("start"), assistant("c0")])

    assert await read_log(blobs, TASK) == [user("start"), assistant("c0")]


async def test_a_hint_joins_the_message_it_followed() -> None:
    # llm_turn shows a hint as text after the tool results. The log keeps it in that message.
    blobs = MemoryBlobStore()
    await append_to_log(blobs, TASK, 0, "reply", [user("start"), assistant("c0")])
    await append_to_log(blobs, TASK, 0, "results", [answer("c0")])
    await append_to_log(blobs, TASK, 1, "reply", [user("Hint from alice: use -x"), assistant("c1")])
    await append_to_log(blobs, TASK, 1, "results", [answer("c1")])

    log = await read_log(blobs, TASK)

    assert log[2].content == [
        ToolResultBlock(tool_use_id="c0", content="ok"),
        TextBlock(text="Hint from alice: use -x"),
    ]
    validate_pairs(log)


async def test_a_turn_that_ended_the_run_may_have_a_reply_and_no_results() -> None:
    blobs = MemoryBlobStore()
    await append_to_log(blobs, TASK, 0, "reply", [user("start"), assistant("c0")])
    await append_to_log(blobs, TASK, 0, "results", [answer("c0")])
    await append_to_log(blobs, TASK, 1, "reply", [assistant("c1")])

    log = await read_log(blobs, TASK)

    assert log[-1] == assistant("c1")


async def test_one_task_does_not_read_the_log_of_another() -> None:
    blobs = MemoryBlobStore()
    await append_to_log(blobs, TASK, 0, "reply", [user("start"), assistant("c0")])

    assert await read_log(blobs, "lh-acme-calc-43") == []


async def test_a_part_that_is_not_json_for_messages_is_an_error_and_not_an_end() -> None:
    # A damaged part must not read as "the log ends here".
    blobs = MemoryBlobStore()
    await append_to_log(blobs, TASK, 0, "reply", [user("start"), assistant("c0")])
    blobs.objects[log_key(TASK, 0, "results")] = b"not json"

    with pytest.raises(ValueError):
        await read_log(blobs, TASK)


async def test_a_blob_store_error_other_than_a_missing_part_goes_up() -> None:
    class Broken(MemoryBlobStore):
        async def get_by_key(self, key: str) -> bytes:
            raise BlobNotFoundError("gone") if key.endswith("0001/reply.json") else OSError("down")

    blobs = Broken()
    with pytest.raises(OSError, match="down"):
        await read_log(blobs, TASK)


# ---------- what a step added ----------
def test_added_messages_are_the_new_tail() -> None:
    before = [user("start")]
    after = [*before, assistant("c0")]

    assert added_messages(before, after) == [assistant("c0")]


def test_added_messages_include_text_added_to_the_last_message() -> None:
    before = [user("start"), assistant("c0"), answer("c0")]
    hinted = Message(role="user", content=[*before[-1].content, TextBlock(text="Hint from a: x")])
    after = [*before[:-1], hinted, assistant("c1")]

    assert added_messages(before, after) == [user("Hint from a: x"), assistant("c1")]


def test_added_messages_of_nothing_before_are_everything() -> None:
    after = [user("start"), assistant("c0")]

    assert added_messages([], after) == after


def test_added_messages_of_no_change_are_empty() -> None:
    before = [user("start")]

    assert added_messages(before, list(before)) == []


def test_added_messages_reject_an_after_that_does_not_extend_before() -> None:
    with pytest.raises(ValueError, match="extend"):
        added_messages([user("a"), assistant("c0")], [user("b")])
