import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from longhaul.contracts import SandboxLimits
from longhaul.settings import AppConfig, EnvSettings, get_settings, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
ENV_PREFIX = "LONGHAUL_"


@pytest.fixture
def fresh_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run every test in an empty directory so a developer's own .env is never read."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            values[name] = value
    return values


def _field_for(env_name: str) -> str | None:
    """Name of the EnvSettings field that reads this environment variable, if any."""
    for name, info in EnvSettings.model_fields.items():
        alias = info.validation_alias
        wanted = alias if isinstance(alias, str) else ENV_PREFIX + name.upper()
        if wanted.upper() == env_name.upper():
            return name
    return None


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


def test_sandbox_limits_reuse_contract_model() -> None:
    cfg = load_config(EXAMPLE)
    assert isinstance(cfg.sandbox.limits, SandboxLimits)
    assert cfg.sandbox.limits.pids == 512


def test_env_example_exports_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exporting the file as it ships (set -a; . .env) must still build the settings."""
    example = _env_example()
    for name, value in example.items():
        monkeypatch.setenv(name, value)

    settings = EnvSettings(_env_file=None)  # type: ignore[call-arg]

    blanks = [name for name, value in example.items() if value == ""]
    assert blanks, "the example is expected to ship blank secrets"
    for name in blanks:
        field = _field_for(name)
        if field is not None:
            assert getattr(settings, field) == EnvSettings.model_fields[field].default, name


def test_env_example_copied_to_dotenv_loads(
    isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The README tells developers to copy .env.example to .env, and Python must read it."""
    for name in _env_example():
        monkeypatch.delenv(name, raising=False)
    shutil.copy(ENV_EXAMPLE, isolated_cwd / ".env")

    settings = EnvSettings()

    assert settings.database_url is not None
    assert settings.temporal_address == _env_example()["LONGHAUL_TEMPORAL_ADDRESS"]
    assert settings.github_app_id is None
    assert settings.admin_token is None


def test_environment_wins_over_dotenv(isolated_cwd: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (isolated_cwd / ".env").write_text(
        "LONGHAUL_TEMPORAL_ADDRESS=from-file:1\nLONGHAUL_TASK_QUEUE=from-file\n", encoding="utf-8"
    )
    monkeypatch.setenv("LONGHAUL_TEMPORAL_ADDRESS", "from-env:2")
    monkeypatch.delenv("LONGHAUL_TASK_QUEUE", raising=False)

    settings = EnvSettings()

    assert settings.temporal_address == "from-env:2"
    assert settings.task_queue == "from-file"


@pytest.mark.parametrize(
    ("env_name", "field"),
    [
        ("LONGHAUL_DATABASE_URL", "database_url"),
        ("LONGHAUL_GITHUB_TOKEN", "github_token"),
        ("LONGHAUL_GITHUB_APP_ID", "github_app_id"),
        ("LONGHAUL_GITHUB_APP_KEY_PATH", "github_app_key_path"),
        ("LONGHAUL_ADMIN_TOKEN", "admin_token"),
        ("LONGHAUL_SCRIPT", "script"),
        ("ANTHROPIC_API_KEY", "anthropic_api_key"),
        ("OPENAI_API_KEY", "openai_api_key"),
        ("OPENAI_BASE_URL", "openai_base_url"),
    ],
)
def test_blank_variable_counts_as_unset(
    monkeypatch: pytest.MonkeyPatch, env_name: str, field: str
) -> None:
    # An empty admin token would compare equal to an empty bearer token, and an empty
    # OPENAI_BASE_URL would reach the SDK as a URL without a host.
    monkeypatch.setenv(env_name, "")

    assert getattr(EnvSettings(), field) is None


def test_env_example_config_path_exists_and_matches_default() -> None:
    configured = Path(_env_example()["LONGHAUL_CONFIG"])

    assert (ROOT / configured).is_file()
    assert configured == EnvSettings.model_fields["config"].default


def test_script_setting_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LONGHAUL_SCRIPT", raising=False)
    assert EnvSettings().script is None

    monkeypatch.setenv("LONGHAUL_SCRIPT", "tests/fixtures/scripts/calc_fix.yaml")
    assert EnvSettings().script == Path("tests/fixtures/scripts/calc_fix.yaml")
