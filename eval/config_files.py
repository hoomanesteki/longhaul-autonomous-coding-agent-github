"""The config files of the evaluation: eval/configs/<name>.yaml (plan section 8.3, LH-903).

A config is a definition of what is being measured. `full` is the product. `naive` is the same
durable stack with the planner, the verifier, memory and the stuck detector off and a sliding
window in place of the two context tiers. `single_shot` is no loop at all: five files and one
call. Each lives in a file of its own, so that the definition can be read, reviewed and hashed.

The runner stores the sha256 of the file in every result (results.TaskResult.config_hash). The
hash is of the bytes of the file, so a comment changes it too. That is deliberate: a number is
tied to the exact text that was in the repo when it was produced, and `git log` on the file says
what changed since.

What the runner can apply and what it can only record. The context mode is a per-run setting
that travels with the task (the workflow memo), so the runner applies it. The planner, the
verifier, memory and the stuck detector are not per-run switches yet: the worker reads them from
its own config, and some of them are not built yet (Phases 5 and 6). The file records what the
config means by them, the result files record them, and the stories that add each switch make the
runner pass it on. A switch that does not exist is off in the stack, which is what `naive` asks.
"""

from __future__ import annotations

import functools
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from eval import results
from longhaul.context.modes import ContextMode

CONFIGS_DIR = Path(__file__).resolve().parent / "configs"
SUFFIX = ".yaml"


class EvalConfig(BaseModel):
    """One config of plan section 8.3. A switch of None means that it does not apply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: results.Config
    description: str = Field(min_length=1)
    # `durable` runs the task as a workflow on the stack. `single_shot` makes one call.
    runner: Literal["durable", "single_shot"]
    planner: bool | None
    verifier: bool | None
    memory: bool | None
    stuck_detector: bool | None
    context_mode: ContextMode | None
    # Single shot only: how many files the retrieval puts in the prompt.
    top_files: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _fits_its_runner(self) -> Self:
        switches = (self.planner, self.verifier, self.memory, self.stuck_detector)
        if self.runner == "single_shot":
            if any(item is not None for item in switches) or self.context_mode is not None:
                raise ValueError(
                    "a single_shot config has no loop, so its planner, verifier, memory, "
                    "stuck_detector and context_mode have to be null"
                )
            if self.top_files is None:
                raise ValueError("a single_shot config needs top_files")
        else:
            if any(item is None for item in switches) or self.context_mode is None:
                raise ValueError(
                    "a durable config has to say true or false for the planner, verifier, "
                    "memory and stuck_detector, and name a context_mode"
                )
            if self.top_files is not None:
                raise ValueError("top_files only goes with the single_shot runner")
        return self


@dataclass(frozen=True)
class LoadedConfig:
    config: EvalConfig
    sha256: str


def switch(value: bool | None) -> results.Switch:
    """A switch as the result files write it. A config without a loop has no memory."""
    return "on" if value else "off"


def load_file(path: Path) -> LoadedConfig:
    """Read a config file and hash its bytes. A mistake raises ValueError that names the file."""
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path.name}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: the file has to be a mapping of settings")
    try:
        config = EvalConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"{path.name}: {exc}") from exc
    if config.name != path.stem:
        raise ValueError(f"{path.name}: the name is {config.name!r}, which has to be {path.stem!r}")
    return LoadedConfig(config, hashlib.sha256(raw).hexdigest())


@functools.cache
def load(name: str) -> LoadedConfig:
    """The config called `name`, from eval/configs.

    The file is read once per process. A run takes its hash when it starts, and a file that
    changes while it runs does not change the hash of the results it is still writing.
    """
    path = CONFIGS_DIR / f"{name}{SUFFIX}"
    if not path.is_file():
        known = ", ".join(sorted(p.stem for p in CONFIGS_DIR.glob(f"*{SUFFIX}")))
        raise ValueError(f"no config {name!r} in {CONFIGS_DIR} (there are: {known})")
    return load_file(path)
