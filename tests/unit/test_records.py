"""The in-memory recorder, which stands in for Postgres in unit tests of the activities.

tests/integration/test_records.py runs the same cases against the SQL recorder, so the two
stay interchangeable.
"""

from datetime import UTC, datetime

import pytest

from longhaul.contracts import BlobRef, Plan, PlanStep, Usage
from longhaul.db.records import (
    CheckpointRecord,
    EventRecord,
    MemoryRecorder,
    PlanRecord,
    Recorder,
    ToolCallRecord,
    TurnRecord,
)

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


def test_a_turn_row_has_the_cache_ratio_of_its_usage() -> None:
    # LH-405: cache_read / (input + cache_read), worked out from the usage, so a caller cannot
    # record a ratio that disagrees with the counts next to it.
    row = turn(usage=Usage(input_tokens=100, cache_read_tokens=300, cache_write_tokens=50))
    assert row.cache_ratio == pytest.approx(0.75)
    assert turn().cache_ratio == 0.0


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


# ---------- checkpoints and events (LH-206) ----------
SHA = "a" * 40


def checkpoint(sha: str = SHA, **fields: object) -> CheckpointRecord:
    values: dict[str, object] = {"task_id": "lh-x", "sha": sha, "kind": "tool", "turn_index": 3}
    return CheckpointRecord(**{**values, **fields})  # type: ignore[arg-type]


async def test_checkpoints_are_kept_in_the_order_they_were_saved() -> None:
    recorder = MemoryRecorder()
    await recorder.record_checkpoint(checkpoint("a" * 40, turn_index=1))
    await recorder.record_checkpoint(checkpoint("b" * 40, turn_index=2))
    assert [(c.sha, c.turn_index) for c in recorder.checkpoints] == [("a" * 40, 1), ("b" * 40, 2)]


async def test_saving_the_same_checkpoint_again_keeps_one_row() -> None:
    # A retried tool call that commits in the same second as its first try makes the same
    # commit, and so the same sha.
    recorder = MemoryRecorder()
    await recorder.record_checkpoint(checkpoint())
    await recorder.record_checkpoint(checkpoint())
    assert len(recorder.checkpoints) == 1


async def test_a_sha_can_be_a_checkpoint_of_two_kinds() -> None:
    recorder = MemoryRecorder()
    await recorder.record_checkpoint(checkpoint(kind="tool"))
    await recorder.record_checkpoint(checkpoint(kind="step", step_id="s1"))
    await recorder.record_checkpoint(checkpoint("b" * 40, kind="tool"))
    assert [(c.kind, c.sha[0]) for c in recorder.checkpoints] == [
        ("tool", "a"),
        ("step", "a"),
        ("tool", "b"),
    ]


@pytest.mark.phase5
async def test_two_steps_done_at_the_same_sha_are_two_checkpoints() -> None:
    # A step marked done with no new work since the one before has the same sha. Each step still
    # has its own row (LH-503), and a row written again is one row.
    recorder = MemoryRecorder()
    await recorder.record_checkpoint(checkpoint(kind="step", step_id="s1"))
    await recorder.record_checkpoint(checkpoint(kind="step", step_id="s2"))
    await recorder.record_checkpoint(checkpoint(kind="step", step_id="s2"))
    assert [(c.kind, c.step_id) for c in recorder.checkpoints] == [("step", "s1"), ("step", "s2")]


@pytest.mark.phase5
async def test_the_step_checkpoints_of_a_task_come_back_in_the_order_they_were_saved() -> None:
    recorder = MemoryRecorder()
    await recorder.record_checkpoint(checkpoint("a" * 40, kind="tool"))
    await recorder.record_checkpoint(checkpoint("b" * 40, kind="step", step_id="s2"))
    await recorder.record_checkpoint(checkpoint("c" * 40, kind="step", step_id="s1"))
    await recorder.record_checkpoint(checkpoint("d" * 40, kind="step", step_id="s1", task_id="y"))

    found = await recorder.step_checkpoints("lh-x")

    assert [(c.step_id, c.sha[0]) for c in found] == [("s2", "b"), ("s1", "c")]
    assert await recorder.step_checkpoints("lh-none") == []


async def test_a_bundle_checkpoint_keeps_its_ref() -> None:
    ref = BlobRef(key=f"tasks/lh-x/ckpt/{SHA}.bundle", sha256="b" * 64, size=9)
    recorder = MemoryRecorder()
    await recorder.record_checkpoint(checkpoint(kind="bundle", bundle_ref=ref))
    assert recorder.checkpoints[0].bundle_ref == ref


def test_a_checkpoint_row_defaults() -> None:
    row = checkpoint()
    assert (row.step_id, row.bundle_ref) == (None, None)


async def test_events_are_kept_in_order_with_their_payload() -> None:
    recorder = MemoryRecorder()
    await recorder.record_event("lh-x", "sandbox_recreated", {"head_sha": SHA})
    await recorder.record_event("lh-y", "hint", {"by": "alice"})
    assert recorder.events == [
        EventRecord("lh-x", "sandbox_recreated", {"head_sha": SHA}),
        EventRecord("lh-y", "hint", {"by": "alice"}),
    ]


async def test_an_event_keeps_a_copy_of_its_payload() -> None:
    recorder = MemoryRecorder()
    payload = {"head_sha": SHA}
    await recorder.record_event("lh-x", "sandbox_recreated", payload)
    payload["head_sha"] = "changed afterwards"
    assert recorder.events[0].payload == {"head_sha": SHA}


async def test_the_same_event_twice_is_two_events() -> None:
    # An event is a log line. An activity that ran twice did the thing twice.
    recorder = MemoryRecorder()
    await recorder.record_event("lh-x", "sandbox_recreated", {})
    await recorder.record_event("lh-x", "sandbox_recreated", {})
    assert len(recorder.events) == 2


# ---------- the last call of a tool (LH-306) ----------
def named(call_id: str, name: str, turn_index: int, task_id: str = "lh-x") -> ToolCallRecord:
    return ToolCallRecord(
        id=call_id, task_id=task_id, turn_index=turn_index, name=name, ok=True, exit_code=0
    )


async def test_the_last_call_of_a_tool_is_the_one_of_the_latest_turn() -> None:
    recorder = MemoryRecorder()
    await recorder.record_tool_call(named("t1", "run_tests", 1))
    await recorder.record_tool_call(named("t3", "run_tests", 3))
    await recorder.record_tool_call(named("t2", "run_tests", 2))
    await recorder.record_tool_call(named("t4", "read_file", 4))

    found = await recorder.last_tool_call("lh-x", "run_tests")

    assert found is not None
    assert found.id == "t3"


async def test_two_calls_in_one_turn_give_the_one_that_was_recorded_last() -> None:
    recorder = MemoryRecorder()
    await recorder.record_tool_call(named("first", "run_tests", 5))
    await recorder.record_tool_call(named("second", "run_tests", 5))

    found = await recorder.last_tool_call("lh-x", "run_tests")

    assert found is not None
    assert found.id == "second"


async def test_no_call_of_the_tool_gives_none() -> None:
    recorder = MemoryRecorder()
    await recorder.record_tool_call(named("t1", "read_file", 1))
    await recorder.record_tool_call(named("t2", "run_tests", 1, task_id="lh-other"))

    assert await recorder.last_tool_call("lh-x", "run_tests") is None


# ---------- the share of the window (LH-406) ----------
async def test_the_largest_share_of_the_window_is_kept() -> None:
    recorder = MemoryRecorder()
    await recorder.record_request_fraction("lh-x", 0.30)
    await recorder.record_request_fraction("lh-x", 0.55)
    await recorder.record_request_fraction("lh-x", 0.40)  # a smaller request changes nothing
    assert recorder.max_fractions == {"lh-x": 0.55}


async def test_the_share_is_kept_for_each_task() -> None:
    recorder = MemoryRecorder()
    await recorder.record_request_fraction("lh-x", 0.50)
    await recorder.record_request_fraction("lh-y", 0.20)
    assert recorder.max_fractions == {"lh-x": 0.50, "lh-y": 0.20}


async def test_recording_the_same_share_again_changes_nothing() -> None:
    # An activity that runs again records what it recorded before.
    recorder = MemoryRecorder()
    await recorder.record_request_fraction("lh-x", 0.50)
    await recorder.record_request_fraction("lh-x", 0.50)
    assert recorder.max_fractions == {"lh-x": 0.50}


# ---------- LH-501: plans ----------
def a_plan(summary: str = "add() subtracts") -> Plan:
    return Plan(
        summary=summary,
        steps=[PlanStep(id="s1", description="Fix add()", done_when="the tests pass")],
        acceptance_checks=["pytest -q"],
    )


async def test_a_plan_is_recorded_as_proposed() -> None:
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    assert recorder.plans == {("lh-x", 1): PlanRecord("lh-x", 1, a_plan(), status="proposed")}


async def test_recording_a_plan_again_keeps_one_row() -> None:
    # Temporal runs store_plan again after a crash. The second run writes what the first wrote.
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    assert len(recorder.plans) == 1


async def test_each_version_of_a_plan_and_each_task_has_a_row_of_its_own() -> None:
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan("first")))
    await recorder.record_plan(PlanRecord("lh-x", 2, a_plan("second")))
    await recorder.record_plan(PlanRecord("lh-y", 1, a_plan("other task")))
    assert sorted(recorder.plans) == [("lh-x", 1), ("lh-x", 2), ("lh-y", 1)]
    assert recorder.plans[("lh-x", 2)].plan.summary == "second"


# ---------- LH-502: the decision on a plan ----------
async def test_a_plan_starts_with_no_decision() -> None:
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    assert recorder.plans[("lh-x", 1)].decided_by is None


async def test_approving_a_plan_records_the_status_and_who_decided() -> None:
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    await recorder.record_plan_decision("lh-x", 1, approved=True, decided_by="alice")
    assert recorder.plans[("lh-x", 1)] == PlanRecord(
        "lh-x", 1, a_plan(), status="approved", decided_by="alice"
    )


async def test_rejecting_a_plan_records_the_status_and_who_decided() -> None:
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    await recorder.record_plan_decision("lh-x", 1, approved=False, decided_by="bob")
    assert recorder.plans[("lh-x", 1)].status == "rejected"
    assert recorder.plans[("lh-x", 1)].decided_by == "bob"


async def test_a_decision_touches_only_its_own_version() -> None:
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan("first")))
    await recorder.record_plan(PlanRecord("lh-x", 2, a_plan("second")))
    await recorder.record_plan_decision("lh-x", 1, approved=False, decided_by="bob")
    assert recorder.plans[("lh-x", 2)].status == "proposed"


async def test_the_first_decision_stands_when_the_activity_runs_again() -> None:
    # Temporal runs decide_plan again after a crash, and a late retry must not turn a rejection
    # into an approval or change who decided.
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    await recorder.record_plan_decision("lh-x", 1, approved=False, decided_by="bob")
    await recorder.record_plan_decision("lh-x", 1, approved=True, decided_by="alice")
    assert recorder.plans[("lh-x", 1)].status == "rejected"
    assert recorder.plans[("lh-x", 1)].decided_by == "bob"


async def test_a_decision_on_a_plan_that_was_never_stored_changes_nothing() -> None:
    recorder = MemoryRecorder()
    await recorder.record_plan_decision("lh-x", 1, approved=True, decided_by="alice")
    assert recorder.plans == {}


async def test_storing_a_plan_again_keeps_its_decision() -> None:
    recorder = MemoryRecorder()
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    await recorder.record_plan_decision("lh-x", 1, approved=True, decided_by="alice")
    await recorder.record_plan(PlanRecord("lh-x", 1, a_plan()))
    assert recorder.plans[("lh-x", 1)].status == "approved"
    assert recorder.plans[("lh-x", 1)].decided_by == "alice"


# ---------- LH-605: the events of a task, read back ----------
async def test_events_of_a_task_come_back_in_the_order_they_were_written() -> None:
    recorder = MemoryRecorder()
    await recorder.record_event("lh-x", "stuck", {"reason": "edit_churn"})
    await recorder.record_event("lh-y", "stuck", {"reason": "no_progress"})
    await recorder.record_event("lh-x", "compaction", {"tokens_before": 9})
    await recorder.record_event("lh-x", "verdict", {"issues": ["a"]})

    got = await recorder.events_of("lh-x", ["stuck", "verdict"])

    assert got == [
        EventRecord("lh-x", "stuck", {"reason": "edit_churn"}),
        EventRecord("lh-x", "verdict", {"issues": ["a"]}),
    ]
    assert await recorder.events_of("lh-x", []) == []
    assert await recorder.events_of("lh-none", ["stuck"]) == []
