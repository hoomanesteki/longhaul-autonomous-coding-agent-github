"""The result files and the summary of an eval run (plan sections 8.5 and 8.6).

One run writes `eval/results/<run_id>/<task_id>.json` for each task and a `summary.json`. Every
number in the README comes from these files, so a file says which run, config, model and commit
it belongs to. A result file is written once. The grader fills `resolved` before it is written.

The fields that later phases fill (compactions, verify rounds, stuck reasons) start at honest
zeros and empty lists. A zero here means "not measured yet" until the phase that counts them has
landed, and the summary does not report them.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

SUMMARY_FILE = "summary.json"
_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

TaskSet = Literal["custom", "swebench"]
Config = Literal["full", "naive", "single_shot"]
Switch = Literal["on", "off"]
ContextMode = Literal["full", "no_compaction", "none"]


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


def read_results(folder: Path) -> list[TaskResult]:
    """Every result in `folder`, ordered by task id. The summary file is not a result."""
    paths = sorted(p for p in folder.glob("*.json") if p.name != SUMMARY_FILE)
    found = [TaskResult.model_validate_json(p.read_text(encoding="utf-8")) for p in paths]
    return sorted(found, key=lambda r: r.task_id)


def _check_one_run(results: list[TaskResult]) -> None:
    for field in ("run_id", "task_set", "config", "model", "provider", "scripted"):
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
