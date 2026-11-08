"""The write_run_report activity, and the report that `deliver` embeds (LH-705).

The report is made from the tables of the task (reports/data.py) and a template
(reports/render.py). The workflow cannot read a table or a template, so an activity does it.

* `write_run_report` builds the report from the rows as they are now, stores it as a blob under
  `tasks/<task>/report/run-report.md` and returns the BlobRef. A report can be asked for again
  while the task runs or after it has ended, and each call replaces the blob with a newer one.
* `stored_or_new` is what `deliver` uses. The body of a pull request has to be the same bytes on
  every attempt, because `create_pr` refuses a reused key with other text, and the rows can grow
  between two attempts (a late event, a compaction call). The first attempt stores the report and
  every later one reads that blob, so the body does not change. It is the stored copy, not the
  rows, that decides.

Failures. A task the database has never heard of, a template that is missing and a task id that
the blob store would refuse are not retried: asking again finds the same thing. A database or a
blob store that cannot be reached raises as it is, and the retry policy runs the activity again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from jinja2 import TemplateError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from longhaul.blobs.base import BlobNotFoundError, BlobStoreBase, report_key
from longhaul.contracts import BlobRef
from longhaul.durable import acts
from longhaul.durable.activities.support import not_retried, small
from longhaul.reports.data import ReportData, read_report_data
from longhaul.reports.render import render_run_report

ReportReader = Callable[[str], Awaitable[ReportData]]


def sql_reader(sessions: async_sessionmaker[AsyncSession]) -> ReportReader:
    """A reader that takes the rows of a task from Postgres."""

    async def read(task_id: str) -> ReportData:
        return await read_report_data(sessions, task_id)

    return read


class RunReports:
    """Builds the report of a task, and keeps a copy of it in the blob store.

    `read` gives the rows of a task. `templates` is the folder that holds run_report.md.j2.
    """

    def __init__(self, read: ReportReader, blobs: BlobStoreBase, *, templates: Path) -> None:
        self._read = read
        self._blobs = blobs
        self._templates = templates

    async def build(self, task_id: str, *, outcome: str | None = None) -> str:
        """The report from the rows as they are now. Nothing is stored."""
        data = await self._read(task_id)
        return render_run_report(data, templates=self._templates, outcome=outcome)

    async def write(self, task_id: str, *, outcome: str | None = None) -> BlobRef:
        """Build the report and store it, replacing an older copy."""
        key = report_key(task_id)
        text = await self.build(task_id, outcome=outcome)
        return await self._blobs.put(text.encode(), key=key)

    async def stored_or_new(self, task_id: str, *, outcome: str | None = None) -> str:
        """The stored report if there is one, else a new one, which is stored first."""
        key = report_key(task_id)
        try:
            return (await self._blobs.get_by_key(key)).decode()
        except BlobNotFoundError:
            await self.write(task_id, outcome=outcome)
        return (await self._blobs.get_by_key(key)).decode()


class ReportActivities:
    """write_run_report, with the reports it builds."""

    def __init__(self, reports: RunReports) -> None:
        self._reports = reports

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.write_run_report]

    @activity.defn(name=acts.WRITE_RUN_REPORT.name)
    async def write_run_report(self, inp: acts.RunReportInput) -> BlobRef:
        inp = small(inp)
        try:
            return small(await self._reports.write(inp.task_id, outcome=inp.outcome))
        except (LookupError, ValueError, TemplateError) as exc:
            raise not_retried(exc) from exc
