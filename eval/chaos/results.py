"""What a chaos run records and what the suite adds up (LH-904).

The suite writes `run-NN.json` as each run ends, so an hour of work is not lost to a crash in
run 17, and `summary.json` when the last run is done. Every number in the report on surviving
crashes comes from these files.

Per run:

* `completed`: the workflow ended with a pull request.
* `diff_equals_reference`: the final diff is the diff of the same task run without a kill.
* `extra_ledger_rows`, `extra_cost_usd`: model calls that were billed again because a kill made
  the activity run again, against the run without a kill.
* `resume_latency_s`: for each kill that was followed by an activity, the seconds from the end of
  the kill to the start of that activity, from the timestamps of Temporal's history.
* `side_effects`, `expected_side_effects`, `duplicate_side_effects`, `missing_side_effects`: the
  comments, label changes and pull requests that the run left on a FakeGitHub, against the ones
  that the lifecycle of a task writes. They are counted from the state of the fake and not from
  our own side_effects table.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from eval.chaos.plan import KILL_TYPES

SUMMARY_FILE = "summary.json"


class SideEffects(BaseModel):
    """The writes to one issue and its repo that a task makes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    comments: int
    label_events: int  # a label added or removed counts once each
    pull_requests: int


class KillResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str  # one of eval.chaos.plan.KILL_TYPES
    landed: bool  # False when the run ended first, or when there was nothing to strike
    place: str | None  # what the worker was doing: llm_turn, tool_exec, between activities ...
    struck_at: AwareDatetime | None
    resume_latency_s: float | None  # None when no activity started after the kill


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: int
    task_id: str
    workflow_id: str
    seed: int
    kills_planned: list[str]
    kills: list[KillResult]
    completed: bool
    outcome: str
    diff_equals_reference: bool
    problems: list[str]  # why the run does not count as survived; empty when it does
    ledger_rows: int
    extra_ledger_rows: int
    cost_usd: float
    extra_cost_usd: float
    resume_latency_s: list[float]
    side_effects: SideEffects
    expected_side_effects: SideEffects
    duplicate_side_effects: int
    missing_side_effects: int
    wall_time_s: float

    @property
    def survived(self) -> bool:
        return (
            self.completed
            and self.diff_equals_reference
            and not self.problems
            and self.duplicate_side_effects == 0
            and self.missing_side_effects == 0
        )


class LatencyStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int
    median_s: float | None
    p95_s: float | None
    max_s: float | None


class Summary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    seed: int
    runs: int
    git_sha: str
    tasks: list[str]
    # Where the model answers came from, for each task: "scripted", or the model that was recorded.
    recordings: dict[str, str]
    compose_project: str  # the private Temporal and Postgres, removed when the suite ended
    started_at: AwareDatetime
    ended_at: AwareDatetime
    wall_time_s: float
    # Seconds the clock of the Docker VM is ahead of this machine's, used for the latency.
    clock_offset_s: float
    completed: int
    completion_rate: float
    diff_matches: int
    survived: int
    kills_planned: dict[str, int]
    kills_landed: dict[str, int]
    extra_ledger_rows: int
    extra_cost_usd: float
    resume_latency: LatencyStats
    resume_latency_by_type: dict[str, LatencyStats]
    duplicate_side_effects: int
    missing_side_effects: int
    runs_with_side_effect_mismatch: int
    results: list[RunResult] = Field(default_factory=list)


def _fields(model: type[BaseModel]) -> tuple[str, ...]:
    return tuple(model.model_fields)


KILL_FIELDS = _fields(KillResult)
RUN_FIELDS = _fields(RunResult)
SUMMARY_FIELDS = _fields(Summary)


def latency_stats(values: Sequence[float]) -> LatencyStats:
    """The median, the 95th percentile (nearest rank) and the largest of some latencies."""
    if not values:
        return LatencyStats(count=0, median_s=None, p95_s=None, max_s=None)
    ordered = sorted(values)
    rank = math.ceil(0.95 * len(ordered))
    return LatencyStats(
        count=len(ordered),
        median_s=statistics.median(ordered),
        p95_s=ordered[rank - 1],
        max_s=ordered[-1],
    )


def _by_type(found: Sequence[RunResult]) -> tuple[dict[str, int], dict[str, int]]:
    planned = dict.fromkeys(KILL_TYPES, 0)
    landed = dict.fromkeys(KILL_TYPES, 0)
    for result in found:
        for kind in result.kills_planned:
            planned[kind] += 1
        for kill in result.kills:
            landed[kill.type] += int(kill.landed)
    return planned, landed


def summarize(
    found: Sequence[RunResult],
    *,
    name: str,
    seed: int,
    git_sha: str,
    tasks: Sequence[str],
    recordings: dict[str, str],
    started_at: datetime,
    ended_at: datetime,
    clock_offset_s: float,
    compose_project: str,
) -> Summary:
    """Add up the runs of one suite."""
    if not found:
        raise ValueError("no runs to summarize")
    planned, landed = _by_type(found)
    latencies: dict[str, list[float]] = {kind: [] for kind in KILL_TYPES}
    for result in found:
        for kill in result.kills:
            if kill.resume_latency_s is not None:
                latencies[kill.type].append(kill.resume_latency_s)
    completed = sum(r.completed for r in found)
    return Summary(
        name=name,
        seed=seed,
        runs=len(found),
        git_sha=git_sha,
        tasks=list(tasks),
        recordings=recordings,
        compose_project=compose_project,
        started_at=started_at,
        ended_at=ended_at,
        wall_time_s=(ended_at - started_at).total_seconds(),
        clock_offset_s=clock_offset_s,
        completed=completed,
        completion_rate=completed / len(found),
        diff_matches=sum(r.diff_equals_reference for r in found),
        survived=sum(r.survived for r in found),
        kills_planned=planned,
        kills_landed=landed,
        extra_ledger_rows=sum(r.extra_ledger_rows for r in found),
        extra_cost_usd=round(sum(r.extra_cost_usd for r in found), 6),
        resume_latency=latency_stats([v for values in latencies.values() for v in values]),
        resume_latency_by_type={kind: latency_stats(values) for kind, values in latencies.items()},
        duplicate_side_effects=sum(r.duplicate_side_effects for r in found),
        missing_side_effects=sum(r.missing_side_effects for r in found),
        runs_with_side_effect_mismatch=sum(
            1 for r in found if r.duplicate_side_effects or r.missing_side_effects
        ),
        results=list(found),
    )


def write_run(folder: Path, result: RunResult) -> Path:
    """Write `run-NN.json`. A file that exists is never replaced."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"run-{result.run:02d}.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(result.model_dump_json(indent=2) + "\n")
    return path


def write_summary(folder: Path, summary: Summary) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / SUMMARY_FILE
    path.write_text(summary.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path
