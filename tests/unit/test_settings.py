import re
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml
from pydantic import ConfigDict, ValidationError

from longhaul.contracts import SandboxLimits
from longhaul.settings import AppConfig, EnvSettings, get_settings, load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "longhaul.example.yaml"
ENV_EXAMPLE = ROOT / ".env.example"
COMPOSE = ROOT / "docker-compose.yml"
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


# Each entry is the path to a section and a realistic typo for a key in it. Every section
# repeats extra="forbid" on its own model, so each one needs its own case.
UNKNOWN_KEY_CASES = [
    pytest.param((), "pricing_fle", id="top level"),
    pytest.param(("poller",), "interval_secnds", id="poller"),
    pytest.param(("repos", 0), "test_comand", id="repos[0]"),
    pytest.param(("repos", 0, "budgets"), "task_usdd", id="repos[0].budgets"),
    pytest.param(("models",), "plannr", id="models"),
    pytest.param(("sandbox",), "imge", id="sandbox"),
    pytest.param(("sandbox", "limits"), "cpu", id="sandbox.limits"),
    pytest.param(("context",), "compact_att", id="context"),
    pytest.param(("loop",), "max_verify_round", id="loop"),
    pytest.param(("stuck",), "no_progress_turn", id="stuck"),
]


@pytest.mark.parametrize(("where", "typo"), UNKNOWN_KEY_CASES)
def test_unknown_key_rejected(tmp_path: Path, where: tuple[str | int, ...], typo: str) -> None:
    data = _example_data()
    section: Any = data
    for step in where:
        section = section[step]
    section[typo] = 30
    path = _write_yaml(tmp_path / "longhaul.yaml", data)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    # Exactly one error, at the typo's own location, so a typo is never silently dropped.
    errors = excinfo.value.errors()
    assert [(error["type"], error["loc"]) for error in errors] == [
        ("extra_forbidden", (*where, typo))
    ]
    assert typo in str(excinfo.value)


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

    # These names are also unknown to the schema, so a bare ValueError check would pass
    # without the scanner. The scanner's own message and exception type prove it ran.
    with pytest.raises(ValueError, match="secret-looking key") as excinfo:
        load_config(path)

    assert not isinstance(excinfo.value, ValidationError)
    assert f"'{key_path}'" in str(excinfo.value)


class _AcceptsAnyKeys(AppConfig):
    """A schema that takes extra keys, like a future free-form settings mapping would."""

    model_config = ConfigDict(extra="allow")


@pytest.fixture
def permissive_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("longhaul.settings.AppConfig", _AcceptsAnyKeys)


SECRET_LOOKING_NAMES = [
    "github_token",
    "githubToken",
    "apiKey",
    "apikey",
    "API_KEY",
    "Admin_Token",
    "clientSecret",
    "client-secret",
    "SECRET",
    "authToken",
    "tokens",
    "keys",
    "secrets",
    "api_keys",
    "token_budget",
    "key_files",
]

# Counts and sizes that contain the word "token" but are not credentials.
COUNT_NAMES = ["max_tokens_per_turn", "tool_output_max_tokens", "window_tokens", "maxTokensPerTurn"]


@pytest.mark.parametrize("name", SECRET_LOOKING_NAMES)
def test_scanner_rejects_secret_looking_names(
    tmp_path: Path, permissive_schema: None, name: str
) -> None:
    # The schema accepts the key here, so only the scanner can reject it.
    data = _example_data()
    data[name] = "x"
    path = _write_yaml(tmp_path / "longhaul.yaml", data)

    with pytest.raises(ValueError, match="secret-looking key") as excinfo:
        load_config(path)

    assert not isinstance(excinfo.value, ValidationError)
    assert f"'{name}'" in str(excinfo.value)


@pytest.mark.parametrize("name", COUNT_NAMES)
def test_scanner_allows_count_keys(tmp_path: Path, permissive_schema: None, name: str) -> None:
    data = _example_data()
    data[name] = 5
    path = _write_yaml(tmp_path / "longhaul.yaml", data)

    assert load_config(path) is not None


def test_scanner_finds_secret_keys_inside_nested_lists(
    tmp_path: Path, permissive_schema: None
) -> None:
    data = _example_data()
    data["repos"][0]["notes"] = [{"label": "fine"}, [{"clientSecret": "x"}]]
    path = _write_yaml(tmp_path / "longhaul.yaml", data)

    with pytest.raises(ValueError, match="secret-looking key") as excinfo:
        load_config(path)

    assert "'repos[0].notes[1][0].clientSecret'" in str(excinfo.value)


def test_repo_is_not_allowlisted_unless_the_yaml_says_so(tmp_path: Path) -> None:
    # Longhaul must never work in a repo it was not invited to, so omitting the key means no.
    data = _example_data()
    data["repos"] = [{"repo": "acme/not-invited"}]
    path = _write_yaml(tmp_path / "longhaul.yaml", data)

    entry = load_config(path).repos[0]

    assert entry.allowlisted is False
    assert load_config(EXAMPLE).repos[0].allowlisted is True


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


def _compose_services() -> dict[str, Any]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    services = data["services"]
    assert isinstance(services, dict)
    return services


def _binds_loopback(port: Any) -> bool:
    if isinstance(port, dict):
        return bool(port.get("host_ip") == "127.0.0.1")
    return str(port).startswith("127.0.0.1:")


def test_compose_publishes_ports_on_loopback_only() -> None:
    """No host address in a port mapping means 0.0.0.0, which exposes the stack to the LAN."""
    published = 0
    exposed: list[str] = []
    for name, service in _compose_services().items():
        for port in service.get("ports", []):
            published += 1
            if not _binds_loopback(port):
                exposed.append(f"{name}: {port}")

    assert published > 0
    assert not exposed, "ports published on every interface:\n" + "\n".join(exposed)


def test_compose_database_password_comes_from_the_environment() -> None:
    password = _compose_services()["postgres"]["environment"]["POSTGRES_PASSWORD"]

    match = re.fullmatch(r"\$\{LH_POSTGRES_PASSWORD:-(?P<default>[^}]+)\}", str(password))
    assert match is not None, f"POSTGRES_PASSWORD is {password!r}"
    # The local defaults must agree, or a fresh clone cannot log in.
    example = _env_example()
    assert example["LH_POSTGRES_PASSWORD"] == match["default"]
    assert urlsplit(example["LONGHAUL_DATABASE_URL"]).password == match["default"]


def test_env_example_lists_every_compose_variable() -> None:
    used = set(re.findall(r"\$\{(LH_[A-Z_]+)", COMPOSE.read_text(encoding="utf-8")))

    assert used
    assert used <= set(_env_example())
