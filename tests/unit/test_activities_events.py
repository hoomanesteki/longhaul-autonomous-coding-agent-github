"""The record_event activity: one row in the events log, written for the workflow.

The workflow cannot write to Postgres, so it asks for each event through this activity. It runs
as a local activity (acts.RECORD_EVENT), and its body is one call to the recorder.
"""

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.db.records import EventRecord, MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.payload_guard import DEFAULT_LIMIT

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
