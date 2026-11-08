"""write_run_report and the stored report that `deliver` embeds (LH-705).

The rows are replaced by a reader that answers from memory, and the blob store is the one in
memory, so the tests are about what the activity does with a report: where it stores it, what
a retry gets back, and which failures are retried. tests/integration/test_run_report.py reads
the rows from Postgres.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.blobs.base import report_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.durable import acts
from longhaul.durable.activities.report import ReportActivities, RunReports
from longhaul.reports.data import EventInfo, ReportData, Spend

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
TASK = "lh-acme-calc-42"
NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


class Reader:
    """A reader that answers with one set of rows, which a test can change, and counts reads."""

    def __init__(self, data: ReportData | Exception) -> None:
        self.data = data
        self.reads = 0

    async def __call__(self, task_id: str) -> ReportData:
        self.reads += 1
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


def rows(**fields: object) -> ReportData:
    return replace(
        ReportData(task_id=TASK, events=[EventInfo(ts=NOW, type="compaction", payload={})]),
        **fields,
    )


def make(reader: Reader, blobs: MemoryBlobStore | None = None) -> ReportActivities:
    reports = RunReports(reader, blobs or MemoryBlobStore(), templates=ROOT / "templates")
    return ReportActivities(reports)


async def run(activities: ReportActivities, task_id: str = TASK) -> object:
    inp = acts.RunReportInput(task_id=task_id)
    return await ActivityEnvironment().run(activities.write_run_report, inp)


def test_the_activity_is_registered_under_its_name_in_acts() -> None:
    activities = make(Reader(rows()))

    assert [fn.__name__ for fn in activities.activities] == [acts.WRITE_RUN_REPORT.name]


async def test_the_report_is_stored_as_markdown_under_the_key_of_the_task() -> None:
    blobs = MemoryBlobStore()

    ref = await run(make(Reader(rows()), blobs))

    assert ref.key == report_key(TASK) == f"tasks/{TASK}/report/run-report.md"
    text = (await blobs.get(ref)).decode()  # type: ignore[arg-type]
    assert text.startswith("### Overview")
    assert "| Turns | 0 |" in text


async def test_the_activity_returns_a_ref_and_no_text() -> None:
    ref = await run(make(Reader(rows(spend=Spend(calls=1)))))

    assert type(ref).__name__ == "BlobRef"


async def test_the_activity_builds_the_report_again_each_time_it_runs() -> None:
    blobs = MemoryBlobStore()
    reader = Reader(rows())
    activities = make(reader, blobs)
    await run(activities)
    reader.data = rows(
        task=None, events=[EventInfo(ts=NOW, type="outcome", payload={"kind": "stuck"})]
    )

    ref = await run(activities)

    assert "| Outcome | stuck |" in (await blobs.get(ref)).decode()  # type: ignore[arg-type]


async def test_a_task_the_database_does_not_know_is_not_retried() -> None:
    with pytest.raises(ApplicationError) as caught:
        await run(make(Reader(LookupError("no rows for lh-nope"))), "lh-nope")

    assert (caught.value.type, caught.value.non_retryable) == ("LookupError", True)


async def test_a_database_that_is_down_is_left_to_the_retry_policy() -> None:
    with pytest.raises(ConnectionError):
        await run(make(Reader(ConnectionError("the database is down"))))


async def test_a_template_that_is_missing_is_not_retried(tmp_path: Path) -> None:
    reports = RunReports(Reader(rows()), MemoryBlobStore(), templates=tmp_path)

    with pytest.raises(ApplicationError) as caught:
        await run(ReportActivities(reports))

    assert caught.value.non_retryable


async def test_a_task_id_the_blob_store_would_refuse_is_not_retried() -> None:
    with pytest.raises(ApplicationError) as caught:
        await run(make(Reader(rows())), "../escape")

    assert caught.value.non_retryable


# ---------- the report that deliver embeds ----------
async def test_deliver_gets_the_stored_report_and_a_retry_gets_the_same_one() -> None:
    reader = Reader(rows())
    reports = RunReports(reader, MemoryBlobStore(), templates=ROOT / "templates")

    first = await reports.stored_or_new(TASK, outcome="pr_opened")
    reader.data = rows(events=[EventInfo(ts=NOW, type="compaction", payload={})] * 3)
    second = await reports.stored_or_new(TASK, outcome="pr_opened")

    assert first == second
    assert reader.reads == 1, "the second call reads the stored report and not the rows"
    assert "| Outcome | pr opened |" in first


async def test_the_activity_replaces_a_report_that_deliver_stored() -> None:
    blobs = MemoryBlobStore()
    reader = Reader(rows())
    reports = RunReports(reader, blobs, templates=ROOT / "templates")
    await reports.stored_or_new(TASK, outcome="pr_opened")
    reader.data = rows(events=[EventInfo(ts=NOW, type="outcome", payload={"kind": "pr_opened"})])

    ref = await run(ReportActivities(reports))

    assert (await blobs.get(ref)).decode() == await reports.stored_or_new(TASK)  # type: ignore[arg-type]
