"""The tool_exec and append_results activities.

tool_exec runs one call in a sandbox, stores what came out and writes the tool_calls row.
append_results answers the tool_use blocks of the last assistant message with the results.
Both run inside temporalio's ActivityEnvironment, with a fake sandbox, the memory blob store and
the memory recorder.
"""

import asyncio
import dataclasses
import shlex
import sys
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.agent.rules import BAD_FINISH, SKIPPED_AFTER_FINISH
from longhaul.agent.turns import CONTROL_RESULT, validate_pairs
from longhaul.blobs.base import blob_key, make_ref
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    BlobRef,
    Message,
    Sandbox,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
TASK = "lh-acme-calc-42"
SHA = "a" * 40
FIRST = Message(role="user", content=[TextBlock(text="go")])


class Lookup:
    """The part of a sandbox manager that tool_exec uses: find a sandbox by its id."""

    def __init__(self, sandbox: Sandbox | None) -> None:
        self.sandbox = sandbox

    async def get(self, sandbox_id: str) -> Sandbox | None:
        if self.sandbox is not None and self.sandbox.id == sandbox_id:
            return self.sandbox
        return None


@pytest.fixture
async def sandbox() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox("sbx-1")
    for f in sorted(CALC.rglob("*")):
        parts = set(f.relative_to(CALC).parts)
        if f.is_file() and not parts & {"REFERENCE.patch", "__pycache__", ".pytest_cache"}:
            await box.write_file(str(f.relative_to(CALC)), f.read_bytes())
    yield box
    await box.destroy()


class Rig:
    def __init__(self, sandbox: Sandbox | None) -> None:
        self.blobs = MemoryBlobStore()
        self.recorder = MemoryRecorder()
        self.activities = ToolActivities(
            default_registry(test_command="pytest -q"),
            Lookup(sandbox),
            self.blobs,
            self.recorder,
        )
        self.beats: list[Any] = []

    def environment(self, attempt: int = 1, heartbeat_timeout_s: float | None = None) -> Any:
        env = ActivityEnvironment()
        timeout = None if heartbeat_timeout_s is None else timedelta(seconds=heartbeat_timeout_s)
        env.info = dataclasses.replace(env.info, attempt=attempt, heartbeat_timeout=timeout)
        env.on_heartbeat = lambda *details: self.beats.append(details)
        return env

    async def exec(self, inp: acts.ToolExecInput, **kwargs: Any) -> Any:
        return await self.environment(**kwargs).run(self.activities.tool_exec, inp)

    async def append(self, inp: acts.AppendResultsInput) -> Any:
        return await self.environment().run(self.activities.append_results, inp)

    async def put_text(self, name: str, text: str, turn: int = 0) -> BlobRef:
        return await self.blobs.put(text.encode(), key=blob_key(TASK, "turns", turn, name))

    async def put_json(self, name: str, model: Any, turn: int = 0) -> BlobRef:
        return await self.blobs.put_json(model, blob_key(TASK, "turns", turn, name))


def exec_input(name: str, call_id: str = "toolu_1", turn: int = 2, **arguments: Any) -> Any:
    return acts.ToolExecInput(
        task_id=TASK,
        turn_index=turn,
        call=acts.ToolCallSummary(id=call_id, name=name, input=arguments),
        pre_sha=SHA,
        sandbox_id="sbx-1",
    )


# ---------- tool_exec ----------
async def test_a_call_runs_in_the_sandbox_and_its_text_is_stored_whole(
    sandbox: FakeSandbox,
) -> None:
    rig = Rig(sandbox)
    result = await rig.exec(exec_input("read_file", path="src/calc/__init__.py"))

    assert result.ok
    assert result.call_id == "toolu_1"
    assert result.output_ref is not None
    stored = (await rig.blobs.get(result.output_ref)).decode()
    assert "return a - b" in stored
    # A short text is its own preview.
    assert result.output == stored
    assert result.output_ref.key == f"tasks/{TASK}/turns/0002/tool-toolu_1-output.txt"


async def test_a_long_text_is_cut_in_the_result_and_whole_in_the_blob(
    sandbox: FakeSandbox,
) -> None:
    rig = Rig(sandbox)
    command = f"{shlex.quote(sys.executable)} -c \"print('y' * 6000)\""
    result = await rig.exec(exec_input("run_command", command=command))

    assert result.output_ref is not None
    stored = (await rig.blobs.get(result.output_ref)).decode()
    assert stored.count("y") == 6000
    assert result.output.count("y") <= acts.OUTPUT_PREVIEW_CHARS
    assert result.output.startswith(stored[: acts.OUTPUT_PREVIEW_CHARS])
    # The preview says that it is one and where the rest is.
    assert result.output_ref.key in result.output
    assert len(result.output) < acts.OUTPUT_PREVIEW_CHARS + 300


async def test_the_result_carries_the_checkpoint_it_started_from_and_no_new_one(
    sandbox: FakeSandbox,
) -> None:
    # LH-206 adds the restore and the commit. Until then a call neither moves nor leaves one.
    rig = Rig(sandbox)
    result = await rig.exec(
        exec_input("edit_file", path="src/calc/__init__.py", old_str="a - b", new_str="a + b")
    )
    assert result.ok
    assert (result.pre_sha, result.post_sha) == (SHA, None)


async def test_a_call_the_model_got_wrong_is_a_result_and_not_an_error(
    sandbox: FakeSandbox,
) -> None:
    rig = Rig(sandbox)
    result = await rig.exec(exec_input("read_file", path="missing.py"))
    assert not result.ok
    assert result.output_ref is not None
    assert (await rig.blobs.get(result.output_ref)).decode() == result.output
    (row,) = rig.recorder.tool_calls.values()
    assert row.ok is False


async def test_an_unknown_tool_is_a_result_too(sandbox: FakeSandbox) -> None:
    result = await Rig(sandbox).exec(exec_input("no_such_tool"))
    assert not result.ok
    assert "unknown tool" in result.output


async def test_the_call_is_recorded_with_its_blobs_and_the_attempt(sandbox: FakeSandbox) -> None:
    rig = Rig(sandbox)
    result = await rig.exec(exec_input("read_file", path="src/calc/__init__.py"), attempt=2)

    (row,) = rig.recorder.tool_calls.values()
    assert (row.id, row.task_id, row.turn_index, row.name) == ("toolu_1", TASK, 2, "read_file")
    assert (row.ok, row.attempt, row.pre_sha, row.post_sha) == (True, 2, SHA, None)
    assert row.output_ref == (result.output_ref.key if result.output_ref else None)
    assert row.duration_ms == result.duration_ms
    # Arguments that arrived inline are stored too, so the row can show what was asked.
    assert row.args_ref == f"tasks/{TASK}/turns/0002/tool-toolu_1-args.json"
    args = make_ref(rig.blobs.objects[row.args_ref], row.args_ref)
    assert await rig.blobs.get_json(args, dict[str, Any]) == {"path": "src/calc/__init__.py"}


async def test_arguments_held_by_reference_are_loaded_for_the_tool(sandbox: FakeSandbox) -> None:
    rig = Rig(sandbox)
    content = "z" * 20_000
    ref = await rig.put_json("args-big.json", {"path": "big.txt", "content": content})
    inp = acts.ToolExecInput(
        task_id=TASK,
        turn_index=2,
        call=acts.ToolCallSummary(id="toolu_1", name="write_file", args_ref=ref),
        pre_sha=SHA,
        sandbox_id="sbx-1",
    )
    result = await rig.exec(inp)

    assert result.ok, result.output
    assert (await sandbox.read_file("big.txt")).decode() == content
    # The arguments already have a blob, so the row points at it.
    (row,) = rig.recorder.tool_calls.values()
    assert row.args_ref == ref.key


async def test_a_sandbox_that_is_gone_fails_the_activity_without_a_retry() -> None:
    rig = Rig(None)
    with pytest.raises(ApplicationError) as caught:
        await rig.exec(exec_input("read_file", path="a.py"))
    assert caught.value.type == "SandboxGoneError"
    assert caught.value.non_retryable
    assert "sbx-1" in str(caught.value)
    assert rig.recorder.tool_calls == {}


async def test_the_activity_heartbeats_while_a_command_runs(sandbox: FakeSandbox) -> None:
    rig = Rig(sandbox)
    result = await rig.exec(exec_input("run_command", command="sleep 0.6"), heartbeat_timeout_s=0.3)
    assert result.ok
    assert len(rig.beats) >= 4


async def test_an_input_over_the_payload_limit_fails_without_a_retry(
    sandbox: FakeSandbox,
) -> None:
    rig = Rig(sandbox)
    with pytest.raises(ApplicationError) as caught:
        await rig.exec(exec_input("write_file", path="x", content="x" * 300_000))
    assert caught.value.non_retryable
    assert caught.value.type == "PayloadTooLargeError"


async def test_the_checkpoint_store_is_a_no_op_until_lh_206(sandbox: FakeSandbox) -> None:
    store = NoCheckpoints()
    assert await store.commit(sandbox, "lh: edit_file turn 0 call toolu_1") is None
    assert await store.restore(sandbox, SHA) is None


# ---------- append_results ----------
def calls_transcript(*blocks: ToolUseBlock) -> list[Message]:
    return [FIRST, Message(role="assistant", content=list(blocks))]


def call_block(call_id: str, name: str = "read_file", **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name=name, input=args)


def append_input(transcript_ref: BlobRef, *results: acts.ToolResultSummary, **fields: Any) -> Any:
    return acts.AppendResultsInput(
        task_id=TASK, turn_index=2, transcript_ref=transcript_ref, results=list(results), **fields
    )


def result_blocks(message: Message) -> list[ToolResultBlock]:
    return [b for b in message.content if isinstance(b, ToolResultBlock)]


async def test_results_answer_the_calls_in_call_order() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a"), call_block("b")))
    out_ref = await rig.append(
        append_input(
            ref,
            # The results arrive in another order than the calls.
            acts.ToolResultSummary(call_id="b", ok=True, output="second"),
            acts.ToolResultSummary(call_id="a", ok=False, output="first"),
        )
    )

    transcript = await rig.blobs.get_json(out_ref, list[Message])
    first, second = result_blocks(transcript[-1])
    assert (first.tool_use_id, first.content, first.is_error) == ("a", "first", True)
    assert (second.tool_use_id, second.content, second.is_error) == ("b", "second", False)
    validate_pairs(transcript)
    assert out_ref.key == f"tasks/{TASK}/turns/0002/transcript-next.json"


async def test_a_text_held_by_reference_is_read_from_the_blob() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a")))
    text = "line\n" * 5000
    stored = await rig.put_text("tool-a-output.txt", text)
    out_ref = await rig.append(
        append_input(ref, acts.ToolResultSummary(call_id="a", ok=True, output_ref=stored))
    )
    (answer,) = result_blocks((await rig.blobs.get_json(out_ref, list[Message]))[-1])
    assert answer.content == text


async def test_a_good_finish_gets_ok_and_the_calls_after_it_say_they_did_not_run() -> None:
    rig = Rig(None)
    transcript = calls_transcript(
        call_block("a"),
        call_block("f", "finish", summary="Done."),
        call_block("c", "edit_file", path="x"),
    )
    ref = await rig.put_json("t.json", transcript)
    out_ref = await rig.append(
        append_input(ref, acts.ToolResultSummary(call_id="a", ok=True, output="contents"))
    )

    ran, ended, skipped = result_blocks((await rig.blobs.get_json(out_ref, list[Message]))[-1])
    assert (ran.content, ran.is_error) == ("contents", False)
    assert (ended.content, ended.is_error) == (CONTROL_RESULT, False)
    assert skipped.is_error
    assert skipped.content == f"not run: {SKIPPED_AFTER_FINISH}"


async def test_a_finish_without_a_summary_is_answered_with_the_error_text() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("f", "finish")))
    out_ref = await rig.append(
        append_input(ref, acts.ToolResultSummary(call_id="f", ok=False, output=BAD_FINISH))
    )
    (answer,) = result_blocks((await rig.blobs.get_json(out_ref, list[Message]))[-1])
    assert (answer.content, answer.is_error) == (BAD_FINISH, True)


async def test_text_from_the_harness_goes_after_the_results() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a")))
    out_ref = await rig.append(
        append_input(
            ref,
            acts.ToolResultSummary(call_id="a", ok=True, output="fine"),
            user_text="Use tools or call finish.",
        )
    )
    last = (await rig.blobs.get_json(out_ref, list[Message]))[-1]
    assert isinstance(last.content[0], ToolResultBlock)
    assert last.content[1] == TextBlock(text="Use tools or call finish.")


async def test_a_nudge_after_a_reply_with_no_calls_is_a_new_user_message() -> None:
    rig = Rig(None)
    transcript = [FIRST, Message(role="assistant", content=[TextBlock(text="Looks fine.")])]
    ref = await rig.put_json("t.json", transcript)
    out_ref = await rig.append(append_input(ref, user_text="Use tools or call finish."))

    stored = await rig.blobs.get_json(out_ref, list[Message])
    assert stored[:-1] == transcript
    assert stored[-1] == Message(role="user", content=[TextBlock(text="Use tools or call finish.")])


async def test_a_nudge_after_an_empty_reply_joins_the_last_user_message() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", [FIRST])
    out_ref = await rig.append(append_input(ref, user_text="Use tools or call finish."))
    stored = await rig.blobs.get_json(out_ref, list[Message])
    assert [m.role for m in stored] == ["user"]
    assert stored[0].content == [*FIRST.content, TextBlock(text="Use tools or call finish.")]


async def test_a_result_for_a_call_that_is_not_in_the_turn_fails_without_a_retry() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a")))
    with pytest.raises(ApplicationError) as caught:
        await rig.append(
            append_input(ref, acts.ToolResultSummary(call_id="zzz", ok=True, output="x"))
        )
    assert caught.value.non_retryable
    assert caught.value.type == "TranscriptError"


async def test_results_for_a_transcript_that_has_no_calls_fail_without_a_retry() -> None:
    rig = Rig(None)
    ref = await rig.put_json(
        "t.json", [FIRST, Message(role="assistant", content=[TextBlock(text="hi")])]
    )
    with pytest.raises(ApplicationError) as caught:
        await rig.append(
            append_input(ref, acts.ToolResultSummary(call_id="a", ok=True, output="x"))
        )
    assert caught.value.type == "TranscriptError"


async def test_hundreds_of_results_by_reference_fit_in_one_payload() -> None:
    rig = Rig(None)
    blocks = [call_block(f"toolu_{n:03d}", path=f"f{n}.py") for n in range(300)]
    ref = await rig.put_json("t.json", calls_transcript(*blocks))
    stored = await rig.put_text("tool-out.txt", "x" * 15_000)
    summaries = [acts.ToolResultSummary(call_id=b.id, ok=True, output_ref=stored) for b in blocks]
    out_ref = await rig.append(append_input(ref, *summaries))
    answers = result_blocks((await rig.blobs.get_json(out_ref, list[Message]))[-1])
    assert len(answers) == 300
    assert all(len(a.content) == 15_000 for a in answers)


async def test_every_input_is_guarded_by_the_payload_limit() -> None:
    rig = Rig(None)
    ref = await rig.put_json("t.json", calls_transcript(call_block("a")))
    with pytest.raises(ApplicationError) as caught:
        await rig.append(append_input(ref, user_text="x" * 300_000))
    assert caught.value.type == "PayloadTooLargeError"
    assert caught.value.non_retryable


async def test_activities_list_what_a_worker_registers() -> None:
    rig = Rig(None)
    names = sorted(a.__name__ for a in rig.activities.activities)
    assert names == ["append_results", "tool_exec"]
    assert asyncio.iscoroutinefunction(rig.activities.tool_exec)
