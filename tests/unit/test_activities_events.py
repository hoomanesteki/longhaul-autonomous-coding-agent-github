"""The record_event activity: one row in the events log, written for the workflow.

The workflow cannot write to Postgres, so it asks for each event through this activity. It runs
as a local activity (acts.RECORD_EVENT), and its body is one call to the recorder.
"""

import dataclasses
from collections.abc import Mapping
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.db.records import EventRecord, MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.payload_guard import DEFAULT_LIMIT
from longhaul.telemetry.metrics import Metrics

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

TASK = "lh-acme-calc-42"


async def record(recorder: MemoryRecorder, inp: acts.RecordEventInput) -> None:
    await ActivityEnvironment().run(EventActivities(recorder).record_event, inp)


async def test_the_event_is_written_with_its_type_and_payload() -> None:
    recorder = MemoryRecorder()
    payload = {"turn_index": 40, "head_sha": "a" * 40, "bundle": None}
    await record(
        recorder, acts.RecordEventInput(task_id=TASK, type="continue_as_new", payload=payload)
    )
    assert recorder.events == [EventRecord(TASK, "continue_as_new", payload)]


async def test_an_event_may_have_no_payload() -> None:
    recorder = MemoryRecorder()
    await record(recorder, acts.RecordEventInput(task_id=TASK, type="resumed"))
    assert recorder.events == [EventRecord(TASK, "resumed", {})]


async def test_the_same_event_twice_is_two_rows() -> None:
    # An event is a log line: it says that the thing happened, and it happened twice.
    recorder = MemoryRecorder()
    inp = acts.RecordEventInput(task_id=TASK, type="hint", payload={"by": "alice"})
    await record(recorder, inp)
    await record(recorder, inp)
    assert len(recorder.events) == 2


async def test_an_oversized_payload_is_refused_for_good() -> None:
    recorder = MemoryRecorder()
    big = acts.RecordEventInput(
        task_id=TASK, type="hint", payload={"text": "x" * (DEFAULT_LIMIT + 1)}
    )
    with pytest.raises(ApplicationError) as caught:
        await record(recorder, big)
    assert caught.value.non_retryable
    assert recorder.events == []


def test_the_activity_list_is_what_a_worker_registers() -> None:
    activities = EventActivities(MemoryRecorder()).activities
    assert [a.__name__ for a in activities] == ["record_event"]


# ---------- the metrics of an event (LH-704) ----------
def in_run(run_id: str) -> ActivityEnvironment:
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_run_id=run_id)
    return env


class DownRecorder(MemoryRecorder):
    """A recorder whose database is down."""

    async def record_event(self, task_id: str, type: str, payload: Mapping[str, Any]) -> None:
        raise ConnectionError("the database is unreachable")


async def test_a_compaction_event_is_counted(counters: Metrics) -> None:
    await record(
        MemoryRecorder(),
        acts.RecordEventInput(task_id=TASK, type="compaction", payload={"tokens_before": 90_000}),
    )
    assert counters.registry.get_sample_value("longhaul_compactions_total") == 1


async def test_a_stuck_event_is_counted_by_its_reason(counters: Metrics) -> None:
    await record(
        MemoryRecorder(),
        acts.RecordEventInput(task_id=TASK, type="stuck", payload={"reason": "edit_churn"}),
    )
    assert counters.registry.get_sample_value("longhaul_stuck_total", {"reason": "edit_churn"}) == 1


async def test_an_event_without_a_metric_is_recorded_and_counts_nothing(
    counters: Metrics,
) -> None:
    before = counters.render()
    recorder = MemoryRecorder()
    await record(recorder, acts.RecordEventInput(task_id=TASK, type="hint"))
    assert len(recorder.events) == 1
    assert counters.render() == before


async def test_an_event_that_was_not_written_is_not_counted(counters: Metrics) -> None:
    # The metric mirrors the events table. A retry of the activity counts the event it writes.
    with pytest.raises(ConnectionError):
        await record(
            DownRecorder(), acts.RecordEventInput(task_id=TASK, type="compaction", payload={})
        )
    assert counters.registry.get_sample_value("longhaul_compactions_total") == 0


async def test_an_outcome_event_counts_the_task_and_ends_its_activity(counters: Metrics) -> None:
    counters.task_started(TASK)
    recorder = MemoryRecorder()
    payload = {"kind": "pr_opened", "repo": "acme/calc", "turns": 5, "cost_usd": 0.12}
    await in_run("run-1").run(
        EventActivities(recorder).record_event,
        acts.RecordEventInput(task_id=TASK, type=acts.OUTCOME_EVENT, payload=payload),
    )

    reading = counters.registry.get_sample_value
    assert reading("longhaul_tasks_total", {"outcome": "pr_opened", "repo": "acme/calc"}) == 1
    assert reading("longhaul_active_tasks") == 0
    assert [e.type for e in recorder.events] == [acts.OUTCOME_EVENT]


async def test_the_same_run_is_counted_once_when_its_workflow_task_is_retried(
    counters: Metrics,
) -> None:
    # A workflow task that fails after the local activity ran is run again, and the activity
    # runs again with it, for the same run. Temporal does not run it again on a replay.
    payload = {"kind": "error", "repo": "acme/calc"}
    inp = acts.RecordEventInput(task_id=TASK, type=acts.OUTCOME_EVENT, payload=payload)
    activities = EventActivities(MemoryRecorder())
    await in_run("run-1").run(activities.record_event, inp)
    await in_run("run-1").run(activities.record_event, inp)

    total = counters.registry.get_sample_value(
        "longhaul_tasks_total", {"outcome": "error", "repo": "acme/calc"}
    )
    assert total == 1


async def test_an_outcome_event_with_a_payload_nobody_expected_counts_as_unknown(
    counters: Metrics,
) -> None:
    await in_run("run-1").run(
        EventActivities(MemoryRecorder()).record_event,
        acts.RecordEventInput(task_id=TASK, type=acts.OUTCOME_EVENT, payload={}),
    )
    total = counters.registry.get_sample_value(
        "longhaul_tasks_total", {"outcome": "unknown", "repo": "unknown"}
    )
    assert total == 1
