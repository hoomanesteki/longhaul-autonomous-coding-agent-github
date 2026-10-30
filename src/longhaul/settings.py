"""Typed settings from environment variables and longhaul.yaml.

Secrets come from the environment only. The YAML loader rejects any key that
looks like a credential, so a secret cannot end up in a checked-in file.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from longhaul.contracts import Budgets, RepoSettings

# Matched against whole words of a key name, so "max_tokens_per_turn" passes
# while "github_token" and "api_key" fail. A plain substring match would reject
# legitimate keys such as max_tokens_per_turn and tool_output_max_tokens.
_SECRET_WORDS = frozenset({"token", "key", "secret"})


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LONGHAUL_", extra="ignore")

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


class SandboxLimits(StrictModel):
    cpus: float
    memory_mb: int
    pids: int


class Sandbox(StrictModel):
    image: str
    limits: SandboxLimits
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
    sandbox: Sandbox
    context: Context
    loop: Loop
    stuck: Stuck
    pricing_file: Path


def _looks_secret(name: str) -> bool:
    words = re.split(r"[^a-z0-9]+", name.lower())
    return any(word in _SECRET_WORDS for word in words)


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
