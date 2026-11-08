"""`longhaul report TASK_ID`: print the run report of a task (LH-705).

The report is built from the database, so the command works for a task that ended, one that is
still running, and one that has no `tasks` row. Here the read is replaced by a fake. The report
itself is tested in tests/unit/test_run_report.py and against Postgres in
tests/integration/test_run_report.py.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from longhaul import cli

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

RUNNER = CliRunner()
TASK = "lh-hoomanesteki-lh-demo-textkit-1"
REPORT = "### Overview\n\n| Turns | 8 |\n"


def fake_read(monkeypatch: pytest.MonkeyPatch, result: str | Exception) -> list[str]:
    asked: list[str] = []

    async def read_report(task_id: str) -> str:
        asked.append(task_id)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(cli, "read_report", read_report)
    return asked


def test_report_prints_the_markdown_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = fake_read(monkeypatch, REPORT)

    result = RUNNER.invoke(cli.app, ["report", TASK])

    assert result.exit_code == 0, result.output
    assert result.stdout == REPORT
    assert asked == [TASK]


def test_report_is_listed_in_the_help() -> None:
    result = RUNNER.invoke(cli.app, ["--help"])

    assert "report" in result.stdout


def test_a_task_the_database_does_not_know_exits_2_with_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_read(monkeypatch, LookupError(f"no task {TASK} in the database"))

    result = RUNNER.invoke(cli.app, ["report", TASK])

    assert result.exit_code == cli.EXIT_ERROR
    assert f"no task {TASK}" in result.output
    assert "Traceback" not in result.output


def test_a_database_that_is_not_configured_exits_2_and_names_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_read(monkeypatch, ValueError("LONGHAUL_DATABASE_URL is not set"))

    result = RUNNER.invoke(cli.app, ["report", TASK])

    assert result.exit_code == cli.EXIT_ERROR
    assert "LONGHAUL_DATABASE_URL" in result.output


def test_a_database_that_cannot_be_reached_exits_2_with_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_read(monkeypatch, OperationalError("select 1", {}, OSError("connection refused")))

    result = RUNNER.invoke(cli.app, ["report", TASK])

    assert result.exit_code == cli.EXIT_ERROR
    assert "Traceback" not in result.output


def test_read_report_without_a_database_url_says_which_setting_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.delenv("LONGHAUL_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        cli,
        "database_url",
        lambda *preferred: (_ for _ in ()).throw(RuntimeError("LONGHAUL_DATABASE_URL is not set")),
    )

    with pytest.raises(ValueError, match="LONGHAUL_DATABASE_URL"):
        asyncio.run(cli.read_report(TASK))
