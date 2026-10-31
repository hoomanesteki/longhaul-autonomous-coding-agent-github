"""The in-memory recorder, which stands in for Postgres in unit tests of the activities.

tests/integration/test_records.py runs the same cases against the SQL recorder, so the two
stay interchangeable.
"""

from datetime import UTC, datetime

import pytest

from longhaul.contracts import Usage
from longhaul.db.records import MemoryRecorder, Recorder, ToolCallRecord, TurnRecord

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def turn(task_id: str = "lh-x", index: int = 0, attempt: int = 1, **fields: object) -> TurnRecord:
    return TurnRecord(
        task_id=task_id,
        turn_index=index,
        attempt=attempt,
        role="coder",
        started_at=T0,
        **fields,  # type: ignore[arg-type]
    )


def call(call_id: str = "toolu_1", *, ok: bool = True, **fields: object) -> ToolCallRecord:
    return ToolCallRecord(
        id=call_id,
        task_id="lh-x",
        turn_index=0,
        name="read_file",
        ok=ok,
        **fields,  # type: ignore[arg-type]
    )


def test_the_memory_recorder_is_a_recorder() -> None:
    recorder: Recorder = MemoryRecorder()
    assert recorder is not None


async def test_attempts_count_the_calls_of_one_turn() -> None:
    recorder = MemoryRecorder()
    assert await recorder.next_attempt("lh-x", 0) == 1
    await recorder.record_turn(turn(attempt=1))
    assert await recorder.next_attempt("lh-x", 0) == 2
    await recorder.record_turn(turn(attempt=2))
    assert await recorder.next_attempt("lh-x", 0) == 3
    # Another turn and another task count from the start.
    assert await recorder.next_attempt("lh-x", 1) == 1
    assert await recorder.next_attempt("lh-y", 0) == 1


async def test_a_call_that_left_no_row_does_not_use_up_an_attempt() -> None:
    # A call that failed before it was billed writes nothing, so its retry takes the same number.
    recorder = MemoryRecorder()
    first = await recorder.next_attempt("lh-x", 0)
    assert await recorder.next_attempt("lh-x", 0) == first


async def test_recording_an_attempt_again_replaces_its_row() -> None:
    recorder = MemoryRecorder()
    await recorder.record_turn(turn(response_ref="old"))
    await recorder.record_turn(turn(response_ref="new"))
    assert [t.response_ref for t in recorder.turns] == ["new"]


async def test_turn_rows_keep_their_usage() -> None:
    recorder = MemoryRecorder()
    usage = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=3, cache_write_tokens=2)
    await recorder.record_turn(turn(usage=usage, cost_usd=0.5, stop_reason="end_turn"))
    (row,) = recorder.turns
    assert row.usage == usage
    assert (row.cost_usd, row.stop_reason) == (0.5, "end_turn")


async def test_a_turn_row_needs_nothing_but_the_call_and_its_start() -> None:
    row = turn()
    assert (row.request_ref, row.response_ref, row.stop_reason) == (None, None, None)
    assert (row.latency_ms, row.ended_at, row.cost_usd) == (None, None, 0.0)
    assert row.usage == Usage()


async def test_a_tool_call_row_is_replaced_by_its_id() -> None:
    recorder = MemoryRecorder()
    await recorder.record_tool_call(call(attempt=1, ok=False))
    await recorder.record_tool_call(call(attempt=2, ok=True))
    (row,) = recorder.tool_calls.values()
    assert (row.attempt, row.ok) == (2, True)


async def test_tool_call_rows_of_different_calls_are_kept() -> None:
    recorder = MemoryRecorder()
    await recorder.record_tool_call(call("toolu_1"))
    await recorder.record_tool_call(call("toolu_2"))
    assert sorted(recorder.tool_calls) == ["toolu_1", "toolu_2"]


def test_a_tool_call_row_defaults() -> None:
    row = call()
    assert (row.attempt, row.duration_ms) == (1, 0)
    assert (row.args_ref, row.output_ref, row.exit_code) == (None, None, None)
    assert (row.pre_sha, row.post_sha) == (None, None)
