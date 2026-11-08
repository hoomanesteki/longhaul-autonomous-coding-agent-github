"""The `tools` section of the config and the settings that go with it (LH-909).

`tools.backend` is `direct` unless the config says `keystone`, so a config written before the
section existed loads and runs as it did. Keystone mode needs an address for Keystone, a token to
sign in with and a key that only Keystone knows for the worker's own MCP server. The secrets come
from the environment, as every secret does (settings.py), and a missing one stops the worker at
the start and names the variable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from longhaul.settings import AppConfig, EnvSettings, Tools, load_config
from longhaul.tools.backend import DirectBackend
from longhaul.tools.keystone_backend import KeystoneBackend, build_backend

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
URL = "http://localhost:8080/mcp"


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty directory, so a developer's own .env is never read."""
    monkeypatch.chdir(tmp_path)
    for name in ("KEYSTONE_TOKEN", "KEYSTONE_TOKEN_FILE", "SANDBOX_MCP_KEY"):
        monkeypatch.delenv(f"LONGHAUL_{name}", raising=False)
    return tmp_path


def example() -> dict[str, Any]:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def config_with(tools: Any, tmp_path: Path) -> AppConfig:
    data = example()
    data["pricing_file"] = str(ROOT / data["pricing_file"])
    if tools is not None:
        data["tools"] = tools
    path = tmp_path / "longhaul.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_config(path)


# ---------- the section ----------
def test_the_example_config_uses_the_direct_backend() -> None:
    cfg = load_config(EXAMPLE)

    assert cfg.tools.backend == "direct"


def test_a_config_without_the_section_is_direct(tmp_path: Path) -> None:
    data = example()
    data.pop("tools", None)
    data["pricing_file"] = str(ROOT / data["pricing_file"])
    path = tmp_path / "old.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")

    assert load_config(path).tools == Tools()


def test_keystone_mode_reads_the_address_and_the_upstream_name(tmp_path: Path) -> None:
    cfg = config_with({"backend": "keystone", "gateway": {"url": URL}}, tmp_path)

    assert cfg.tools.backend == "keystone"
    assert cfg.tools.gateway is not None
    assert cfg.tools.gateway.url == URL
    # The id Keystone gives an upstream has no hyphen, so the default is the MCP server's name
    # with an underscore.
    assert cfg.tools.gateway.upstream == "longhaul_sandbox"
    assert cfg.tools.gateway.listen_host == "127.0.0.1"


def test_keystone_mode_without_an_address_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="tools.gateway"):
        config_with({"backend": "keystone"}, tmp_path)


def test_an_unknown_backend_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        config_with({"backend": "gateway"}, tmp_path)


def test_a_key_that_is_not_in_the_section_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        config_with({"backend": "direct", "colour": "red"}, tmp_path)


# ---------- building the backend ----------
def test_direct_builds_the_direct_backend() -> None:
    backend = build_backend(Tools(), EnvSettings())

    assert isinstance(backend, DirectBackend)


def keystone_tools() -> Tools:
    return Tools.model_validate({"backend": "keystone", "gateway": {"url": URL}})


def test_keystone_mode_needs_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LONGHAUL_SANDBOX_MCP_KEY", "k" * 20)

    with pytest.raises(ValueError, match="LONGHAUL_KEYSTONE_TOKEN"):
        build_backend(keystone_tools(), EnvSettings())


def test_keystone_mode_needs_the_key_of_the_workers_own_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LONGHAUL_KEYSTONE_TOKEN", "t")

    with pytest.raises(ValueError, match="LONGHAUL_SANDBOX_MCP_KEY"):
        build_backend(keystone_tools(), EnvSettings())


async def test_keystone_mode_builds_the_keystone_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LONGHAUL_KEYSTONE_TOKEN", "t")
    monkeypatch.setenv("LONGHAUL_SANDBOX_MCP_KEY", "k" * 20)

    backend = build_backend(keystone_tools(), EnvSettings())

    assert isinstance(backend, KeystoneBackend)
    await backend.aclose()


def test_a_token_file_is_read_again_for_every_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "keystone-token"
    token_file.write_text("first\n", encoding="utf-8")
    monkeypatch.setenv("LONGHAUL_KEYSTONE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("LONGHAUL_SANDBOX_MCP_KEY", "k" * 20)
    backend = build_backend(keystone_tools(), EnvSettings())
    assert isinstance(backend, KeystoneBackend)

    assert backend.current_token() == "first"
    token_file.write_text("second\n", encoding="utf-8")
    assert backend.current_token() == "second"


def test_the_key_and_the_token_are_secrets_in_the_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LONGHAUL_KEYSTONE_TOKEN", "super-secret-token")
    monkeypatch.setenv("LONGHAUL_SANDBOX_MCP_KEY", "super-secret-key-0000")

    shown = repr(EnvSettings())

    assert "super-secret-token" not in shown
    assert "super-secret-key-0000" not in shown


# ---------- the upstream the runbook tells the owner to register ----------
def runbook_upstream() -> dict[str, Any]:
    """The `longhaul_sandbox` upstream block of docs/runbook.md, as the owner would paste it."""
    text = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    blocks = text.split("```yaml")[1:]
    for block in blocks:
        body = block.split("```", 1)[0]
        if "credential_env" in body:
            return dict(yaml.safe_load(body)[0])
    raise AssertionError("the runbook has no upstream block")


@pytest.mark.parametrize("tool", ["run_command", "run_tests"])
def test_the_runbook_upstream_waits_as_long_as_a_command_may_run(tool: str) -> None:
    from longhaul.durable.acts import TOOL_TIMEOUT_S

    # Keystone stops waiting for an upstream after 30 s unless the tool says more. A test run of
    # 45 s would come back as a timeout, every time, and count as a failure of the model.
    settings = runbook_upstream()["tools"][tool]

    assert settings["timeout_s"] >= TOOL_TIMEOUT_S
