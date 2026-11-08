"""Write the dry-run result fixtures that the report tests read (LH-905).

    uv run python scripts/make_eval_result_fixtures.py

The files go to tests/fixtures/eval_results/<run_id>/. They have the shape of the files that
eval/run.py writes, but every one of them says `provider: scripted`, so no number in them says
anything about a model. They exist so that the report builder can be tested end to end on a set of
runs that touches every section of the page: the two headline sets on three configs, a second run
of `full` on the custom set, the compaction and memory ablations, and a chaos summary.

The numbers are drawn from fixed seeds and the timestamps are fixed, so a second run of this
script rewrites the same bytes. The chaos summary is not made here: it is the summary of the first
full chaos suite (eval/results/chaos-2026-11-07), whose recordings were scripted as well.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import results  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "eval_results"
CHAOS_SOURCE = ROOT / "eval" / "results" / "chaos-2026-11-07" / "summary.json"
CHAOS_RUN = "chaos-dryrun"

SONNET = "scripted-sonnet"
HAIKU = "scripted-haiku"
GIT_SHA = "0123456789abcdef0123456789abcdef01234567"
START = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
LONG_CATEGORIES = {"refactor", "dependency", "flaky"}

STUCK_REASONS = [
    "the same failing test came back after six edits",
    "no new file was read or changed for eight turns",
]


HAIKU_FULL: dict[str, Any] = {"model": HAIKU, "task_set": "custom", "config": "full", "seed": 14}


def config_hash(config: str) -> str:
    return hashlib.sha256((ROOT / "eval" / "configs" / f"{config}.yaml").read_bytes()).hexdigest()


def custom_tasks() -> list[tuple[str, str]]:
    """(id, category) of the fifteen custom tasks."""
    rows = yaml.safe_load((ROOT / "eval" / "custom" / "tasks.yaml").read_text(encoding="utf-8"))
    return [(row["id"], row["category"]) for row in rows]


def swebench_tasks() -> list[str]:
    return [f"fixture__swe-{n:03d}" for n in range(1, 41)]


def make_run(
    run_id: str,
    *,
    task_set: results.TaskSet,
    config: results.Config,
    model: str,
    tasks: list[str],
    seed: int,
    skill: float,
    cost: float,
    turns: int,
    input_tokens: int,
    context_mode: results.ContextMode | None = None,
    memory: results.Switch = "on",
    ungraded: int = 0,
) -> None:
    """One run. `skill` is the chance that a task is solved, drawn from the seed per task.

    Tasks are drawn in order from one generator, so the run of a weaker config solves roughly the
    tasks of a stronger one and some others: the discordant pairs of a McNemar test are mixed.
    """
    rng = random.Random(seed)
    difficulty = random.Random(1).sample(range(len(tasks)), len(tasks))
    folder = OUT / run_id
    if folder.exists():
        shutil.rmtree(folder)
    for index, task_id in enumerate(tasks):
        easy = difficulty[index] / len(tasks)  # the same hidden difficulty in every run
        solved = easy + rng.uniform(-0.2, 0.2) < skill
        outcome, resolved, grading, reasons, paused = outcome_of(
            solved, config, rng, last=index >= len(tasks) - ungraded
        )
        started = START + timedelta(minutes=7 * index)
        wall = round(rng.uniform(0.6, 1.4) * (90 if config == "single_shot" else 600), 1)
        spent = round(cost * rng.uniform(0.6, 1.4), 4)
        tokens = results.Tokens(
            input=int(input_tokens * rng.uniform(0.7, 1.3)),
            output=int(1000 * turns * rng.uniform(0.7, 1.3)),
            cache_read=0 if config == "single_shot" else int(input_tokens * rng.uniform(1.5, 3)),
            cache_write=int(input_tokens * 0.1),
        )
        results.write_result(
            folder,
            results.TaskResult(
                run_id=run_id,
                task_id=task_id,
                workflow_id=f"lh-eval-{run_id}-{task_id}",
                task_set=task_set,
                config=config,
                config_hash=config_hash(config),
                seed=seed,
                run_set=None,
                model=model,
                git_sha=GIT_SHA,
                provider="scripted",
                scripted=True,
                memory=memory,
                context_mode=context_mode,
                outcome=outcome,
                outcome_summary=summary_of(outcome),
                resolved=resolved,
                grading=grading,
                cost_usd=spent,
                tokens=tokens,
                cache_ratio=results.cache_ratio(tokens),
                started_at=started,
                ended_at=started + timedelta(seconds=wall),
                wall_time_s=wall,
                turns=1 if config == "single_shot" else max(2, int(turns * rng.uniform(0.6, 1.4))),
                stuck_reasons=reasons,
                intervention_needed=paused,
            ),
        )
    results.write_summary(folder, summarize_folder(folder))


def outcome_of(
    solved: bool, config: results.Config, rng: random.Random, *, last: bool
) -> tuple[str, bool | None, str, list[str], bool]:
    """(outcome, resolved, grading, stuck reasons, paused) of one task."""
    if last:
        return "pr_opened", None, "not graded yet", [], False
    if solved:
        return "pr_opened", True, "all tests passed", [], False
    kind = rng.choice(["wrong", "wrong", "stuck", "budget", "error"])
    if kind == "stuck" and config == "full":
        return "stuck", False, "no patch to grade", [rng.choice(STUCK_REASONS)], True
    if kind == "budget":
        return "budget_exhausted", False, "no patch to grade", [], config == "full"
    if kind == "error" and config != "single_shot":
        return "error", False, "no patch to grade", [], False
    return "pr_opened", False, f"{rng.randint(1, 4)} tests failed", [], False


def summary_of(outcome: str) -> str:
    return {
        "pr_opened": "Opened a pull request.",
        "stuck": "Stopped and asked for help.",
        "budget_exhausted": "Ran out of budget.",
        "error": "The task ended with an error.",
    }.get(outcome, outcome)


def summarize_folder(folder: Path) -> results.Summary:
    found = results.read_results(folder)
    first = min(r.started_at for r in found)
    last = max(r.ended_at for r in found)
    return results.summarize(found, started_at=first, ended_at=last)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.parse_args()
    swe = swebench_tasks()
    custom = [task_id for task_id, _ in custom_tasks()]
    long_set = [task_id for task_id, category in custom_tasks() if category in LONG_CATEGORIES]

    for config, skill, cost, turns in (
        ("single_shot", 0.3, 0.05, 1),
        ("naive", 0.45, 1.8, 30),
        ("full", 0.7, 2.4, 40),
    ):
        make_run(
            f"swe-{config}",
            task_set="swebench",
            config=config,
            tasks=swe,
            seed=40,
            skill=skill,
            cost=cost,
            turns=turns,
            ungraded=1 if config == "full" else 0,
            model=SONNET,
            input_tokens=40_000,
        )
    for name, config, seed, skill, cost, turns in (
        ("custom-single_shot", "single_shot", 15, 0.3, 0.04, 1),
        ("custom-naive", "naive", 15, 0.45, 1.0, 20),
        ("custom-full", "full", 15, 0.75, 1.3, 25),
        ("custom-full-2", "full", 16, 0.7, 1.3, 25),
    ):
        make_run(
            name,
            task_set="custom",
            config=config,
            tasks=custom,
            seed=seed,
            skill=skill,
            cost=cost,
            turns=turns,
            model=SONNET,
            input_tokens=40_000,
        )
    make_run(
        "long-compact",
        tasks=long_set,
        skill=0.7,
        cost=0.5,
        turns=30,
        input_tokens=30_000,
        context_mode="full",
        **HAIKU_FULL,
    )
    make_run(
        "long-no-compaction",
        tasks=long_set,
        skill=0.55,
        cost=0.8,
        turns=30,
        input_tokens=50_000,
        context_mode="no_compaction",
        **HAIKU_FULL,
    )
    make_run(
        "memory-on",
        tasks=custom,
        skill=0.7,
        cost=0.4,
        turns=18,
        input_tokens=25_000,
        memory="on",
        **HAIKU_FULL,
    )
    make_run(
        "memory-off",
        tasks=custom,
        skill=0.6,
        cost=0.45,
        turns=24,
        input_tokens=25_000,
        memory="off",
        **HAIKU_FULL,
    )
    target = OUT / CHAOS_RUN
    target.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(CHAOS_SOURCE, target / "summary.json")


if __name__ == "__main__":
    main()
