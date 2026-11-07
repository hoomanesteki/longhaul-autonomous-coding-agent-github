"""The result files and the summary of an eval run (plan sections 8.5 and 8.6).

One run writes `eval/results/<run_id>/<task_id>.json` for each task and a `summary.json`. Every
number in the README comes from these files, so a file says which run, config, model and commit
it belongs to. A result file is written once. The grader fills `resolved` before it is written,
except in the SWE-bench set, where the official harness grades the whole run afterwards and
`set_verdict` puts its answers into the files.

The fields that later phases fill (compactions, verify rounds, stuck reasons) start at honest
zeros and empty lists. A zero here means "not measured yet" until the phase that counts them has
landed, and the summary does not report them.

Which definition produced a number (LH-903). Every file holds the sha256 of the config file it ran
with (eval/configs/<config>.yaml) and the seed of the run, so a result can be traced to the exact
settings of its config. Runs that are meant to be compared share a `run_set`, and the runner
refuses to mix models inside one (`models_in_run_set`).
"""

from __future__ import annotations

import os
import re
import statistics
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

SUMMARY_FILE = "summary.json"
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

TaskSet = Literal["custom", "swebench"]
Config = Literal["full", "naive", "single_shot"]
CONFIG_NAMES: tuple[str, ...] = get_args(Config)
Switch = Literal["on", "off"]
ContextMode = Literal["full", "no_compaction", "none", "sliding"]
# The sha256 of a config file, in lowercase hex.
CONFIG_HASH = r"^[0-9a-f]{64}$"


class Tokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


def cache_ratio(tokens: Tokens) -> float:
    """cache_read / (input + cache_read). Tokens written to the cache are not part of it."""
    reads = tokens.input + tokens.cache_read
    return tokens.cache_read / reads if reads else 0.0


def tokens_per_task(tokens: Tokens) -> int:
    """input + output + cache_read, the plan's "tokens per task"."""
    return tokens.input + tokens.output + tokens.cache_read


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_id: str
    workflow_id: str
    task_set: TaskSet
    config: Config
    # The sha256 of eval/configs/<config>.yaml as it was when the run started, and the seed.
    config_hash: str = Field(pattern=CONFIG_HASH)
    seed: int
    # The name the operator gave to the runs that are compared with each other, if any.
    run_set: str | None = None
    # The run was started with --allow-mixed-models. The runner compares models only inside a
    # run set, so this is what tells a later comparison that a pair on two models was meant.
    allow_mixed_models: bool = False
    model: str
    git_sha: str
    # Where the answers came from. `scripted` is true when a script played them: such a run is
    # free and fixes what its script fixes, so it says nothing about a model. `provider` is what
    # --provider asked for. None means the worker's own, which the runner cannot see.
    provider: str | None
    scripted: bool
    # What the operator asked for on the command line. The worker takes memory and context
    # settings from its own config, so these two describe how it was started.
    memory: Switch = "on"
    context_mode: ContextMode | None = None

    outcome: str  # an OutcomeKind value
    outcome_summary: str
    # True or False once the grader has run. None when it could not run, which the summary
    # counts as ungraded.
    resolved: bool | None
    grading: str  # one line from the grader, or why the task was not graded

    cost_usd: float
    tokens: Tokens
    cache_ratio: float
    started_at: AwareDatetime
    ended_at: AwareDatetime
    wall_time_s: float
    turns: int

    compactions: int = 0
    verify_rounds: int = 0
    stuck_reasons: list[str] = Field(default_factory=list)
    # The task paused at least once, so a person would have had to answer.
    intervention_needed: bool = False
    # Set when the runner ended the task: the reason it gave.
    stopped_by_runner: str | None = None


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    task_set: TaskSet
    config: Config
    config_hash: str = Field(pattern=CONFIG_HASH)
    seed: int
    run_set: str | None = None
    allow_mixed_models: bool = False
    model: str
    provider: str | None
    scripted: bool
    started_at: AwareDatetime
    ended_at: AwareDatetime
    wall_time_s: float
    tasks: list[str]
    # Every started task counts, errors and timeouts included.
    attempted: int
    resolved: int
    ungraded: int
    resolve_rate: float
    cost_usd: float
    cost_per_resolved: float | None
    median_wall_time_s_resolved: float | None
    median_tokens_per_task: float
    median_cache_ratio: float
    intervention_needed: int
    intervention_needed_rate: float


def check_task_id(task_id: str) -> str:
    """The id as a file name, or ValueError when it could leave the run folder."""
    if not _TASK_ID.fullmatch(task_id) or ".." in task_id:
        raise ValueError(f"invalid task id {task_id[:80]!r} for a result file")
    return task_id


def write_result(folder: Path, result: TaskResult) -> Path:
    """Write `<task_id>.json` in `folder`. A file that exists is never replaced."""
    name = check_task_id(result.task_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(result.model_dump_json(indent=2) + "\n")
    return path


def set_verdict(folder: Path, task_id: str, *, resolved: bool | None, grading: str) -> TaskResult:
    """Change `resolved` and `grading` of a result that was written without a verdict.

    The SWE-bench harness grades a whole run after it, so its verdicts reach the files later.
    Nothing else in the file changes. The new file replaces the old one in one step, so a reader
    never sees half of it.
    """
    path = folder / f"{check_task_id(task_id)}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no result for task {task_id} in {folder}")
    current = TaskResult.model_validate_json(path.read_text(encoding="utf-8"))
    changed = current.model_copy(update={"resolved": resolved, "grading": grading})
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_text(changed.model_dump_json(indent=2) + "\n", encoding="utf-8")
    os.replace(scratch, path)
    return changed


def read_results(folder: Path) -> list[TaskResult]:
    """Every result in `folder`, ordered by task id. The summary file is not a result."""
    paths = sorted(p for p in folder.glob("*.json") if p.name != SUMMARY_FILE)
    found = [TaskResult.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]
    return sorted(found, key=lambda r: r.task_id)


def models_in_run_set(results_dir: Path, run_set: str) -> dict[str, str]:
    """The model of each run in `results_dir` that belongs to `run_set`, by run id.

    A run is a folder of result files. Its first result file says which run set and model it had,
    and a run in progress has results as soon as its first task is done. A folder that holds no
    readable result, such as an empty one or the output of another tool, is not a run and is
    skipped.
    """
    if not results_dir.is_dir():
        return {}
    found: dict[str, str] = {}
    for folder in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        for path in sorted(folder.glob("*.json")):
            if path.name == SUMMARY_FILE:
                continue
            try:
                first = TaskResult.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError):
                continue
            if first.run_set == run_set:
                found[folder.name] = first.model
            break
    return found


def check_comparable(runs: Sequence[Summary]) -> None:
    """Refuse to put runs on more than one model side by side, unless a run allowed it.

    The runner checks the models of a run set when a run starts, and only for runs that name a
    set. A report that pairs two runs calls this, so a pair that nobody put in a set is held to
    the same rule (plan section 8.4). `--allow-mixed-models` on any of the runs says that the
    operator meant it.
    """
    models = {run.run_id: run.model for run in runs}
    if len(set(models.values())) > 1 and not any(run.allow_mixed_models for run in runs):
        named = ", ".join(f"{run_id} used {model}" for run_id, model in sorted(models.items()))
        raise ValueError(
            f"these runs are on more than one model ({named}), and none of them was started "
            "with --allow-mixed-models. A comparison needs one model (plan section 8.4)"
        )


def _check_one_run(results: list[TaskResult]) -> None:
    fields = (
        "run_id",
        "task_set",
        "config",
        "config_hash",
        "seed",
        "run_set",
        "allow_mixed_models",
    )
    for field in (*fields, "model", "provider", "scripted"):
        values = {str(getattr(r, field)) for r in results}
        if len(values) != 1:
            raise ValueError(
                f"the results belong to more than one run: {field} is {sorted(values)}"
            )


def summarize(results: list[TaskResult], *, started_at: datetime, ended_at: datetime) -> Summary:
    """The numbers of plan section 8.5 that one run can give on its own.

    Confidence intervals and paired tests need two runs, so the report (LH-905) computes them.
    """
    if not results:
        raise ValueError("no results to summarize")
    _check_one_run(results)
    first = results[0]
    resolved = [r for r in results if r.resolved]
    attempted = len(results)
    cost = round(sum(r.cost_usd for r in results), 6)
    interventions = sum(1 for r in results if r.intervention_needed)
    return Summary(
        run_id=first.run_id,
        task_set=first.task_set,
        config=first.config,
        config_hash=first.config_hash,
        seed=first.seed,
        run_set=first.run_set,
        allow_mixed_models=first.allow_mixed_models,
        model=first.model,
        provider=first.provider,
        scripted=first.scripted,
        started_at=started_at,
        ended_at=ended_at,
        wall_time_s=(ended_at - started_at).total_seconds(),
        tasks=[r.task_id for r in results],
        attempted=attempted,
        resolved=len(resolved),
        ungraded=sum(1 for r in results if r.resolved is None),
        resolve_rate=len(resolved) / attempted,
        cost_usd=cost,
        cost_per_resolved=round(cost / len(resolved), 6) if resolved else None,
        median_wall_time_s_resolved=(
            statistics.median(r.wall_time_s for r in resolved) if resolved else None
        ),
        median_tokens_per_task=statistics.median(tokens_per_task(r.tokens) for r in results),
        median_cache_ratio=statistics.median(r.cache_ratio for r in results),
        intervention_needed=interventions,
        intervention_needed_rate=interventions / attempted,
    )


def write_summary(folder: Path, summary: Summary) -> Path:
    """Write `summary.json`. It is rebuilt from the result files, so it may be replaced."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SUMMARY_FILE
    path.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
