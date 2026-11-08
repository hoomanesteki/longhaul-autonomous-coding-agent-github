"""`longhaul api`: start the console API with uvicorn (LH-801).

uvicorn is replaced by a recorder, so no port is opened. The app itself is exercised in
tests/integration/test_api.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn
from typer.testing import CliRunner

from longhaul import cli
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.unit, pytest.mark.phase8]

RUNNER = CliRunner()


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []

    def run(app: Any, **options: Any) -> None:
        # The app reads the address it listens on from the settings when it starts, which is
        # after this call, so what the environment holds now is what the app will see.
        calls.append({"app": app, "env_host": os.environ.get("LONGHAUL_API_HOST"), **options})

    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.delenv("LONGHAUL_API_HOST", raising=False)
    monkeypatch.delenv("LONGHAUL_API_PORT", raising=False)
    get_settings.cache_clear()
    yield calls
    get_settings.cache_clear()


def test_api_serves_the_app_on_the_port_of_the_settings(
    served: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LONGHAUL_API_PORT", "8123")
    get_settings.cache_clear()

    result = RUNNER.invoke(cli.app, ["api"])

    assert result.exit_code == 0, result.output
    assert len(served) == 1
    assert served[0]["app"] == "longhaul.api.app:create_app"
    assert served[0]["factory"] is True
    assert (served[0]["host"], served[0]["port"]) == ("127.0.0.1", 8123)


def test_api_options_win_over_the_settings(served: list[dict[str, Any]]) -> None:
    result = RUNNER.invoke(cli.app, ["api", "--host", "0.0.0.0", "--port", "9001"])

    assert result.exit_code == 0, result.output
    assert (served[0]["host"], served[0]["port"]) == ("0.0.0.0", 9001)


def test_the_app_is_told_the_host_it_listens_on(served: list[dict[str, Any]]) -> None:
    # /metrics is open only on a server bound to this machine (LH-807), and the app decides that
    # from LONGHAUL_API_HOST. A --host option has to reach it, or an API started with
    # --host 0.0.0.0 would still think it was local.
    RUNNER.invoke(cli.app, ["api", "--host", "0.0.0.0"])

    assert served[0]["env_host"] == "0.0.0.0"
    assert "LONGHAUL_API_HOST" not in os.environ


def test_the_environment_is_restored_after_the_server_stops(
    served: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LONGHAUL_API_HOST", "127.0.0.1")
    get_settings.cache_clear()

    RUNNER.invoke(cli.app, ["api", "--host", "0.0.0.0"])

    assert served[0]["env_host"] == "0.0.0.0"
    assert os.environ["LONGHAUL_API_HOST"] == "127.0.0.1"


def test_api_is_listed_in_the_help() -> None:
    result = RUNNER.invoke(cli.app, ["--help"])

    assert "api" in result.output


def test_the_settings_default_to_a_port_on_this_machine() -> None:
    fields = EnvSettings.model_fields

    assert fields["api_host"].default == "127.0.0.1"
    assert fields["api_port"].default == 8000
