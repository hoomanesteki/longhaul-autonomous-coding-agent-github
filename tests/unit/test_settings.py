from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from longhaul.settings import AppConfig, EnvSettings, get_settings, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"


@pytest.fixture
def fresh_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _example_data() -> dict[str, Any]:
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_example_config_loads() -> None:
    config = load_config(EXAMPLE)

    assert isinstance(config, AppConfig)
    assert config.poller.interval_seconds == 60
    assert config.repos[0].repo == "hoomanesteki/lh-demo-calc"
    assert config.repos[0].allowlisted is True
    assert config.repos[0].budgets.task_usd == 5
    assert config.sandbox.limits.pids == 512
    assert config.context.window_tokens == 200000
    assert config.loop.max_tokens_per_turn == 8192


def test_unknown_key_rejected(tmp_path: Path) -> None:
    data = _example_data()
    data["poller"]["interval_secnds"] = 30
    path = _write_yaml(tmp_path / "longhaul.yaml", data)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "interval_secnds" in str(excinfo.value)


@pytest.mark.parametrize(
    ("key_path", "patch"),
    [
        ("repos[0].github_token", lambda d: d["repos"][0].update(github_token="ghp_x")),
        ("repos[0].budgets.api_key", lambda d: d["repos"][0]["budgets"].update(api_key="x")),
        ("sandbox.blob_secret_key", lambda d: d["sandbox"].update(blob_secret_key="x")),
        ("Admin_Token", lambda d: d.update(Admin_Token="x")),
    ],
)
def test_secret_keys_rejected_in_yaml(tmp_path: Path, key_path: str, patch: Any) -> None:
    data = _example_data()
    patch(data)
    path = _write_yaml(tmp_path / "longhaul.yaml", data)

    with pytest.raises(ValueError) as excinfo:
        load_config(path)

    assert key_path in str(excinfo.value)


def test_env_overrides(monkeypatch: pytest.MonkeyPatch, fresh_settings_cache: None) -> None:
    monkeypatch.setenv("LONGHAUL_TEMPORAL_ADDRESS", "temporal.internal:7233")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-value")

    settings = EnvSettings()
    assert settings.temporal_address == "temporal.internal:7233"
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "sk-test-value"

    get_settings.cache_clear()
    cached = get_settings()
    assert cached.temporal_address == "temporal.internal:7233"
    assert "sk-test-value" not in repr(cached)


def test_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LONGHAUL_TEMPORAL_ADDRESS",
        "LONGHAUL_TEMPORAL_NAMESPACE",
        "LONGHAUL_TASK_QUEUE",
        "LONGHAUL_PROVIDER",
        "LONGHAUL_CONFIG",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = EnvSettings()
    assert settings.temporal_address == "localhost:7233"
    assert settings.temporal_namespace == "default"
    assert settings.task_queue == "longhaul"
    assert settings.provider == "scripted"
    assert settings.config == Path("config/longhaul.example.yaml")
