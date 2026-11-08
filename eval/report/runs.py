"""Load the runs a report is built from, and decide which table each one belongs to (LH-905).

A run is a folder under eval/results. A folder of `<task_id>.json` files is an eval run (the
runner's output, eval/results.py). A folder whose `summary.json` has the kill counts of a chaos
suite is a chaos run (eval/chaos/results.py). Nothing else is read: every number on the page is
computed from these files and from nothing the operator typed.

Which table a run lands in is decided by what the files say about it, not by its name. The
config, the task set, the context mode and the memory switch of the result files tell a plain
run from an ablation. Two things are not in the files, so the rules below cover them:

* The headline model. The first plain run in the order of the command line names it, and the
  headline tables hold the plain runs on that model. List the headline runs first.
* The long-task subset of the compaction ablation. It is not stored anywhere. The ablation is
  paired on the tasks that both of its runs attempted, so the operator gets the subset by running
  the ablation on it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from eval import results
from eval.chaos import results as chaos_results
from eval.results import TaskResult

# A task that ends without a pull request, a stuck report or a budget comment leaves nothing on
# the issue (plan section 8.5, "silent failure").
REPORTED_OUTCOMES = frozenset({"pr_opened", "stuck", "budget_exhausted"})
# The fields that must be the same in every file of a run.
_ONE_PER_RUN = (
    "run_id",
    "task_set",
    "config",
    "config_hash",
    "model",
    "scripted",
    "memory",
    "context_mode",
    "git_sha",
)
# The configs of the headline tables, in the order the tables list them.
HEADLINE_CONFIGS = ("single_shot", "naive", "full")


class ReportError(ValueError):
    """The runs cannot be turned into a page, and the message says what to change."""


@dataclass(frozen=True)
class EvalRun:
    """The result files of one eval run."""

    run_id: str
    results: tuple[TaskResult, ...]

    @property
    def first(self) -> TaskResult:
        return self.results[0]

    @property
    def config(self) -> str:
        return self.first.config

    @property
    def task_set(self) -> str:
        return self.first.task_set

    @property
    def model(self) -> str:
        return self.first.model

    @property
    def scripted(self) -> bool:
        return self.first.scripted

    @property
    def context_mode(self) -> str | None:
        return self.first.context_mode

    @property
    def memory(self) -> str:
        return self.first.memory

    @property
    def attempted(self) -> int:
        return len(self.results)

    @property
    def resolved(self) -> int:
        return sum(1 for r in self.results if r.resolved)

    @property
    def ungraded(self) -> int:
        return sum(1 for r in self.results if r.resolved is None)

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(r.task_id for r in self.results)

    @property
    def silent_failures(self) -> int:
        return sum(1 for r in self.results if r.outcome not in REPORTED_OUTCOMES)

    def solved(self, task_id: str) -> bool:
        """True when the task was resolved. A task that was not graded counts as not resolved."""
        return any(r.task_id == task_id and r.resolved is True for r in self.results)

    @property
    def plain(self) -> bool:
        """The run is a measurement of its config as the plan defines it, not an ablation."""
        if self.config != "full":
            return True
        return self.context_mode in (None, "full") and self.memory == "on"


@dataclass(frozen=True)
class ChaosRun:
    run_id: str
    summary: chaos_results.Summary

    @property
    def scripted(self) -> bool:
        return any(source == "scripted" for source in self.summary.recordings.values())


@dataclass(frozen=True)
class Loaded:
    eval_runs: tuple[EvalRun, ...] = ()
    chaos_runs: tuple[ChaosRun, ...] = ()


@dataclass(frozen=True)
class Pairing:
    """An ablation run and the plain run it is compared with, on the tasks both attempted."""

    with_run: EvalRun
    without_run: EvalRun
    task_ids: tuple[str, ...]


@dataclass
class Layout:
    headline_model: str | None = None
    swebench: dict[str, list[EvalRun]] = field(default_factory=dict)
    custom: dict[str, list[EvalRun]] = field(default_factory=dict)
    compaction: list[Pairing] = field(default_factory=list)
    memory: list[Pairing] = field(default_factory=list)


def _check_one_run(run_id: str, found: list[TaskResult]) -> None:
    for name in _ONE_PER_RUN:
        values = {str(getattr(r, name)) for r in found}
        if len(values) != 1:
            raise ReportError(
                f"the results in {run_id} belong to more than one run: {name} is {sorted(values)}"
            )


def _is_chaos(folder: Path) -> bool:
    path = folder / chaos_results.SUMMARY_FILE
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "kills_planned" in data


def load_run(results_dir: Path, run_id: str) -> EvalRun | ChaosRun:
    folder = results_dir / run_id
    if not folder.is_dir():
        raise ReportError(f"there is no run {run_id!r}: {folder} is not a folder")
    try:
        if _is_chaos(folder):
            path = folder / chaos_results.SUMMARY_FILE
            return ChaosRun(
                run_id, chaos_results.Summary.model_validate_json(path.read_text("utf-8"))
            )
        found = results.read_results(folder)
    except (ValidationError, OSError) as error:
        raise ReportError(f"the files of run {run_id} cannot be read: {error}") from error
    if not found:
        raise ReportError(f"run {run_id} has no result files in {folder}")
    _check_one_run(run_id, found)
    if found[0].run_id != run_id:
        raise ReportError(f"the files in {folder} say run_id {found[0].run_id!r}, not {run_id!r}")
    return EvalRun(run_id, tuple(found))


def load_runs(results_dir: Path, run_ids: Sequence[str]) -> Loaded:
    if len(set(run_ids)) != len(run_ids):
        raise ReportError("a run id is listed twice")
    eval_runs: list[EvalRun] = []
    chaos_runs: list[ChaosRun] = []
    for run_id in run_ids:
        run = load_run(results_dir, run_id)
        if isinstance(run, ChaosRun):
            chaos_runs.append(run)
        else:
            eval_runs.append(run)
    return Loaded(tuple(eval_runs), tuple(chaos_runs))


def _partner(ablation: EvalRun, candidates: Sequence[EvalRun]) -> Pairing | None:
    """The plain full run on the model of `ablation` that shares the most tasks with it.

    A tie goes to the run listed first. The most tasks wins because an operator may run the
    ablation on a subset of the tasks of a plain run, and the plain run that covers it best is
    the one it was meant to be compared with.
    """
    best: Pairing | None = None
    for candidate in candidates:
        if candidate is ablation or not candidate.plain or candidate.config != "full":
            continue
        if candidate.model != ablation.model or candidate.task_set != ablation.task_set:
            continue
        covered = set(candidate.task_ids)
        shared = tuple(t for t in ablation.task_ids if t in covered)
        if shared and (best is None or len(shared) > len(best.task_ids)):
            best = Pairing(candidate, ablation, shared)
    return best


def arrange(eval_runs: Sequence[EvalRun]) -> Layout:
    """Sort the runs into the tables. A run that fits no table is only listed under "Runs used"."""
    layout = Layout()
    plain = [r for r in eval_runs if r.plain]
    if plain:
        layout.headline_model = plain[0].model
    for run in plain:
        if run.model != layout.headline_model:
            continue
        table = layout.swebench if run.task_set == "swebench" else layout.custom
        table.setdefault(run.config, []).append(run)
    for run in eval_runs:
        if run.config != "full" or run.plain:
            continue
        pairing = _partner(run, eval_runs)
        if pairing is None:
            continue
        if run.context_mode == "no_compaction":
            layout.compaction.append(pairing)
        elif run.memory == "off":
            layout.memory.append(pairing)
    return layout
