"""Typed settings from environment variables and longhaul.yaml.

Secrets come from the environment only. The YAML loader rejects any key that
looks like a credential, so a secret cannot end up in a checked-in file.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from longhaul.contracts import Budgets, EffortLevel, RepoSettings, SandboxLimits

# A key is secret-looking when its name contains one of these after it is lower-cased and
# stripped of separators. Matching inside the flattened name catches camelCase (githubToken),
# names without separators (apikey) and plurals (api_keys), which whole-word matching missed.
_SECRET_FRAGMENTS = ("token", "key", "secret")

# Count and size settings that contain "token" but are not credentials, in flattened form.
# Add a name here only for a setting that holds a number, never a value that could be a secret.
_COUNT_KEYS = frozenset(
    {"abovetokens", "maxtokensperturn", "maxtokensretry", "tooloutputmaxtokens", "windowtokens"}
)


class EnvSettings(BaseSettings):
    # .env is read from the working directory; real environment variables win over it.
    # env_ignore_empty makes a blank assignment (a copied .env.example is full of them)
    # mean "unset". Without it a blank admin token becomes SecretStr(""), a blank base URL
    # becomes "", and a blank app id fails to parse.
    model_config = SettingsConfigDict(
        env_prefix="LONGHAUL_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: SecretStr | None = None
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"
    task_queue: str = "longhaul"
    blob_endpoint: str | None = None
    blob_bucket: str | None = None
    blob_access_key: SecretStr | None = None
    blob_secret_key: SecretStr | None = None
    provider: Literal["anthropic", "openai", "scripted", "replay"] = "scripted"
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, validation_alias="OPENAI_BASE_URL")
    github_token: SecretStr | None = None
    github_app_id: int | None = None
    github_app_key_path: Path | None = None
    admin_token: SecretStr | None = None
    config: Path = Path("config/longhaul.example.yaml")
    # Script file for the scripted provider; the provider factory reads it (LH-197).
    script: Path | None = None
    # Where the worker keeps one bare repo per task. Unset means ~/.longhaul/ckpt (ADR-0006).
    ckpt_dir: Path | None = None
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BudgetsEntry(Budgets):
    model_config = ConfigDict(extra="forbid")


class RepoEntry(RepoSettings):
    model_config = ConfigDict(extra="forbid")

    budgets: BudgetsEntry = Field(default_factory=BudgetsEntry)
    allowlisted: bool = False


class Poller(StrictModel):
    interval_seconds: int
    label: str


class ModelIds(StrictModel):
    planner: str
    coder: str
    verifier: str
    compactor: str


class EffortLevels(StrictModel):
    """Reasoning effort per role. It goes out as CallOptions.effort on every call."""

    planner: EffortLevel
    coder: EffortLevel
    verifier: EffortLevel
    compactor: EffortLevel


class SandboxLimitsEntry(SandboxLimits):
    model_config = ConfigDict(extra="forbid")


class Sandbox(StrictModel):
    image: str
    limits: SandboxLimitsEntry
    command_timeout_s: int


class Context(StrictModel):
    window_tokens: int
    compact_at: float
    hard_limit: float
    keep_recent_turns: int
    elide_tool_outputs_after_turns: int
    tool_output_max_tokens: int


class Loop(StrictModel):
    max_verify_rounds: int
    continue_as_new_every_turns: int
    max_tokens_per_turn: int
    # The cap for the one retry of a turn that stopped on max_tokens.
    max_tokens_retry: int

    @model_validator(mode="after")
    def _retry_cap_is_not_below_the_first_cap(self) -> Self:
        if self.max_tokens_retry < self.max_tokens_per_turn:
            raise ValueError(
                f"max_tokens_retry ({self.max_tokens_retry}) must not be below "
                f"max_tokens_per_turn ({self.max_tokens_per_turn}), or the retry could not "
                "finish a reply that the first try cut off"
            )
        return self


class Stuck(StrictModel):
    no_progress_turns: int
    repeated_failure_count: int
    churn_edits: int
    consecutive_tool_errors: int
    hint_wait_hours: int


class AppConfig(StrictModel):
    poller: Poller
    repos: list[RepoEntry]
    models: ModelIds
    effort: EffortLevels
    sandbox: Sandbox
    context: Context
    loop: Loop
    stuck: Stuck
    pricing_file: Path


def _looks_secret(name: str) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    return flat not in _COUNT_KEYS and any(fragment in flat for fragment in _SECRET_FRAGMENTS)


def _reject_secret_keys(node: Any, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            key_path = f"{path}.{key}" if path else str(key)
            if _looks_secret(str(key)):
                raise ValueError(
                    f"secret-looking key '{key_path}' is not allowed in YAML; "
                    "set the value in the environment instead"
                )
            _reject_secret_keys(value, key_path)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _reject_secret_keys(item, f"{path}[{index}]")


def load_config(path: Path | str) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    _reject_secret_keys(raw)
    return AppConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> EnvSettings:
    return EnvSettings()
