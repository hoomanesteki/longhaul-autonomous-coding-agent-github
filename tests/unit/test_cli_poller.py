"""The `longhaul poller` commands: install, pause, resume and trigger (LH-307).

Temporal is replaced: the commands get a fake client, and the schedule functions record what
they were asked. tests/integration/test_poller.py runs the same functions on a real server.

The tests are plain functions on purpose: each command starts its own event loop with
asyncio.run, which cannot happen inside a running one.
"""

import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from temporalio.service import RPCError, RPCStatusCode
from typer.testing import CliRunner, Result

from longhaul import cli
from longhaul.durable import acts, schedule

pytestmark = [pytest.mark.unit, pytest.mark.phase3]

ROOT = Path(__file__).resolve().parents[2]
RUNNER = CliRunner()
QUEUE = "lh-unit-queue"
CLIENT = object()


class Calls:
    """What the commands asked of the schedule functions."""

    def __init__(self) -> None:
        self.log: list[tuple[str, dict[str, Any]]] = []
        self.install_result = schedule.Installed.CREATED
        self.error: Exception | None = None

    def note(self, name: str, **kwargs: Any) -> None:
        self.log.append((name, kwargs))
        if self.error is not None:
            raise self.error


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Calls:
    for name in list(os.environ):
        if name.startswith("LONGHAUL_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)
    config = yaml.safe_load((ROOT / "config" / "longhaul.example.yaml").read_text())
    config["poller"]["interval_seconds"] = 45
    path = tmp_path / "longhaul.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("LONGHAUL_CONFIG", str(path))
    monkeypatch.setenv("LONGHAUL_TASK_QUEUE", QUEUE)

    seen = Calls()

    async def open_client(settings: Any) -> object:
        assert settings.task_queue == QUEUE
        return CLIENT

    async def install(client: Any, *, interval_seconds: int, task_queue: str) -> schedule.Installed:
        assert client is CLIENT
        seen.note("install", interval_seconds=interval_seconds, task_queue=task_queue)
        return seen.install_result

    async def pause(client: Any, *, note: str = "") -> None:
        assert client is CLIENT
        seen.note("pause", note=note)

    async def resume(client: Any, *, note: str = "") -> None:
        assert client is CLIENT
        seen.note("resume", note=note)

    async def trigger(client: Any) -> None:
        assert client is CLIENT
        seen.note("trigger")

    monkeypatch.setattr(cli, "open_client", open_client)
    monkeypatch.setattr(schedule, "install_poller", install)
    monkeypatch.setattr(schedule, "pause_poller", pause)
    monkeypatch.setattr(schedule, "resume_poller", resume)
    monkeypatch.setattr(schedule, "trigger_poller", trigger)
    return seen


def invoke(*args: str) -> Result:
    return RUNNER.invoke(cli.app, ["poller", *args])


def test_install_uses_the_interval_of_the_config_and_the_task_queue_of_the_settings(
    calls: Calls,
) -> None:
    result = invoke("install")

    assert result.exit_code == 0, result.output
    assert calls.log == [("install", {"interval_seconds": 45, "task_queue": QUEUE})]


@pytest.mark.parametrize(
    ("outcome", "words"),
    [
        (schedule.Installed.CREATED, "created"),
        (schedule.Installed.UPDATED, "updated"),
        (schedule.Installed.UNCHANGED, "unchanged"),
    ],
)
def test_install_says_what_it_did(calls: Calls, outcome: schedule.Installed, words: str) -> None:
    calls.install_result = outcome

    result = invoke("install")

    assert words in result.output
    assert acts.POLLER_SCHEDULE_ID in result.output
    assert "every 45 s" in result.output
    assert QUEUE in result.output


@pytest.mark.parametrize("command", ["pause", "resume", "trigger"])
def test_each_management_command_acts_on_the_schedule(calls: Calls, command: str) -> None:
    result = invoke(command)

    assert result.exit_code == 0, result.output
    assert [name for name, _ in calls.log] == [command]
    assert acts.POLLER_SCHEDULE_ID in result.output


def test_pause_passes_on_the_reason(calls: Calls) -> None:
    result = invoke("pause", "--note", "deploying the worker")

    assert result.exit_code == 0, result.output
    assert calls.log == [("pause", {"note": "deploying the worker"})]


def test_trigger_says_a_poll_that_is_already_open_makes_it_a_no_op(calls: Calls) -> None:
    result = invoke("trigger")

    assert "already running" in result.output


def test_a_missing_schedule_is_a_one_line_error_with_exit_code_2(calls: Calls) -> None:
    calls.error = schedule.ScheduleMissingError("the schedule x does not exist")

    result = invoke("pause")

    assert result.exit_code == 2
    assert "error: the schedule x does not exist" in result.output
    assert "Traceback" not in result.output


def test_an_unreachable_server_is_a_one_line_error_with_exit_code_2(calls: Calls) -> None:
    calls.error = RPCError("connection refused", RPCStatusCode.UNAVAILABLE, b"")

    result = invoke("install")

    assert result.exit_code == 2
    assert "error: temporal: connection refused" in result.output


def test_a_bad_interval_in_the_config_is_reported_before_the_server_is_called(
    calls: Calls, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = yaml.safe_load((ROOT / "config" / "longhaul.example.yaml").read_text())
    config["poller"]["interval_seconds"] = 0
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("LONGHAUL_CONFIG", str(bad))
    calls.error = ValueError("the interval must be at least one second")

    result = invoke("install")

    assert result.exit_code == 2
    assert "interval" in result.output


def test_the_poller_group_lists_its_four_commands() -> None:
    result = RUNNER.invoke(cli.app, ["poller", "--help"])

    assert result.exit_code == 0
    for name in ("install", "pause", "resume", "trigger"):
        assert name in result.output
