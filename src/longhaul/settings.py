"""Typed settings from environment variables and longhaul.yaml.

Secrets come from the environment only. The YAML loader rejects any key that
looks like a credential, so a secret cannot end up in a checked-in file.
"""

import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from longhaul.context.modes import ContextMode
from longhaul.context.tokens import result_cap
from longhaul.contracts import Budgets, EffortLevel, RepoSettings, SandboxLimits

# The identity of the commits on a pull request branch, written the way git writes an author.
# squash.parse_author checks it when the worker starts.
DEFAULT_GIT_AUTHOR = "longhaul <longhaul@users.noreply.github.com>"

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
    # Where GitHub is. Unset means api.github.com and github.com. A GitHub Enterprise server sets
    # the API and web URLs, and the end-to-end test of LH-308 points them at a FakeGitHub and a
    # folder of bare repos. The web URL is the base of `<base>/<owner>/<name>.git`, which is what
    # the host clones from and pushes to. The clone base (LH-904) names a folder or server for
    # clones only and wins over the web URL; the chaos suite points a worker at a FakeGitHub it
    # serves on this machine, so it can count what a run wrote.
    github_api_url: str | None = None
    github_web_url: str | None = None
    github_clone_base: str | None = None
    admin_token: SecretStr | None = None
    config: Path = Path("config/longhaul.example.yaml")
    # Script file for the scripted provider; the provider factory reads it (LH-197).
    script: Path | None = None
    # Where the worker keeps one bare repo per task. Unset means ~/.longhaul/ckpt (ADR-0006).
    ckpt_dir: Path | None = None
    # Recorded model calls (LH-209). With provider "replay" the provider plays this folder. With
    # `record` on, the provider of the run is wrapped and saves every call into it.
    recordings_dir: Path | None = None
    record: bool = False
    # Author and committer of the commits on a pull request branch (LH-306).
    git_author: str = DEFAULT_GIT_AUTHOR
    # Where the Jinja templates are, such as pr_body.md.j2. Unset means templates/ in the checkout.
    templates_dir: Path | None = None
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, validation_alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    # Where the worker serves its Prometheus metrics (telemetry/metrics.py). The port is the one
    # docker/prometheus/prometheus.yml scrapes. The host keeps the endpoint on this machine; a
    # Prometheus in Docker on Linux reaches the host through the docker bridge and needs
    # 0.0.0.0 (docs/runbook.md). Port 0 lets the system choose a free port.
    metrics_port: int = Field(default=9464, ge=0, le=65535)
    metrics_host: str = "127.0.0.1"
    # Where `longhaul api` serves the console API (LH-801). The host keeps it on this machine
    # until the console has a login (LH-807); a container that publishes the port needs 0.0.0.0.
    api_host: str = "127.0.0.1"
    api_port: int = Field(default=8000, ge=0, le=65535)
    # The built console (console/dist) that the API serves at `/` (LH-802). Empty means the API
    # serves the API only, as it does next to the Vite dev server.
    console_dir: str = ""
    # The interactive docs of the API at /docs, /redoc and /openapi.json (LH-891). Off by default:
    # none of the three asks for the token, and /docs loads Swagger UI from a CDN on the origin
    # where the console keeps the token. For local development only.
    api_docs: bool = False


# longhaul/settings.py -> longhaul -> src -> the checkout.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def config_path(settings: EnvSettings) -> Path:
    """The config file: the one named by LONGHAUL_CONFIG, found from any working directory.

    The default is a path relative to the repo root. Run from another folder it would be
    missing, so the checkout this package lives in is tried next.
    """
    path = settings.config
    if path.is_absolute() or path.exists():
        return path
    bundled = PROJECT_ROOT / path
    return bundled if bundled.exists() else path


def templates_path(settings: EnvSettings) -> Path:
    """The folder of the Jinja templates: LONGHAUL_TEMPLATES_DIR, else templates/ of the checkout.

    The worker image copies the folder next to the code and sets the variable, because an
    installed package does not sit inside the checkout.
    """
    return (
        settings.templates_dir if settings.templates_dir is not None else PROJECT_ROOT / "templates"
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BudgetsEntry(Budgets):
    model_config = ConfigDict(extra="forbid")


class RepoEntry(RepoSettings):
    model_config = ConfigDict(extra="forbid")

    budgets: BudgetsEntry = Field(default_factory=BudgetsEntry)
    allowlisted: bool = False
    # A task of this repo that ends stuck opens a draft pull request with the work it has (LH-507,
    # decision D4). Off by default: a draft nobody asked for is noise on a busy repo. The flag is
    # a worker setting and not part of RepoSettings, which is a contract. The poller hands it to
    # the task when it starts it (durable/acts.OPEN_DRAFT_MEMO_KEY).
    open_draft_on_stuck: bool = False


# The fields of a RepoEntry that RepoSettings, the contract a task runs with, does not have. They
# are read from the config by the worker when it starts a task, so the `repo_settings` rows and
# the TaskSpec leave them out.
WORKER_ONLY_FIELDS = frozenset({"open_draft_on_stuck"})


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
    # Which of the two tiers run (LH-406). `full` is what the product does, and the default keeps
    # every config written before the mode existed on that behavior. `sliding` runs neither tier
    # and drops the oldest turns at the hard limit, as the naive baseline does (LH-903). A run can
    # name its own mode (`longhaul run --context-mode`), and that wins over this one.
    mode: ContextMode = ContextMode.FULL

    def for_window(self, window_tokens: int | None) -> Self:
        """The policy of a run that asks for a window of its own (`longhaul run --window`).

        Only two numbers change. The window is the one asked for, and the cap for one tool result
        is cut to fit it (context.tokens.result_cap): a result of 4,000 tokens, which the config
        allows in a window of 200,000, would fill half of a window of 8,000, and the room that
        llm_turn keeps for the results of a turn would never be there. The thresholds are shares
        of the window, so they follow it. None gives the policy as it is.
        """
        if window_tokens is None:
            return self
        return self.model_copy(
            update={
                "window_tokens": window_tokens,
                "tool_output_max_tokens": result_cap(self.tool_output_max_tokens, window_tokens),
            }
        )


class Loop(StrictModel):
    # The failed reviews after which a task ends as stuck. The workflow keeps a copy of the number
    # in acts.MAX_VERIFY_ROUNDS, because it cannot read this file, and a unit test keeps them equal.
    max_verify_rounds: int = Field(ge=1)
    continue_as_new_every_turns: int
    max_tokens_per_turn: int
    # The cap for the one retry of a turn that stopped on max_tokens.
    max_tokens_retry: int
    # The turns the planner gets to submit its plan. The workflow keeps a copy of the number in
    # acts.PLANNER_MAX_TURNS, because it cannot read this file, and a unit test keeps them equal.
    planner_max_turns: int = Field(ge=1)

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


class Telemetry(StrictModel):
    # Prompts, tool arguments and tool output can hold code and secrets. Spans carry names,
    # sizes and timings only, unless this is on (plan LH-703).
    capture_content: bool


class Memory(StrictModel):
    """What the agent remembers about a repo between tasks (Phase 6, ADR-0010).

    `embedder` says which model makes the vectors that notes are found by. `local` is the real
    one and needs the `memory` extra (`uv sync --extra memory`). `hash` needs nothing installed
    and finds notes by shared words only, which is for tests and CI. The vectors of the two
    cannot be compared, so a database holds the rows of one, and a worker refuses to start on a
    table that another embedder wrote. With `enabled` off the worker builds no store, keeps no
    repo map or verified commands, and the memory block of the prompt stays empty.

    `mode` is the switch for a store that stays (LH-607, ADR-0010). With `off` the memory block
    of the prompt is empty, no repo map is built and no lessons are drafted, but the commands are
    still verified and the first one that runs the tests still becomes the test command of a task.
    Those commands are Longhaul's own findings at the default branch and not notes from a task, so
    a run without notes keeps them, and a run with and a run without memory (Phase 9) differ in
    the notes only. It defaults to `on`, so a config written before the setting loads as it did.
    YAML 1.1 reads an unquoted `on` and `off` as booleans, so those are accepted as the words.
    """

    enabled: bool
    embedder: Literal["local", "hash"]
    mode: Literal["on", "off"] = "on"

    @field_validator("mode", mode="before")
    @classmethod
    def _yaml_booleans(cls, value: object) -> object:
        if isinstance(value, bool):
            return "on" if value else "off"
        return value


class AppConfig(StrictModel):
    poller: Poller
    repos: list[RepoEntry]
    models: ModelIds
    effort: EffortLevels
    sandbox: Sandbox
    context: Context
    loop: Loop
    stuck: Stuck
    telemetry: Telemetry
    memory: Memory
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
