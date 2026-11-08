"""The plan of the full chaos suite: which task, and what is killed (LH-904).

`chaos.py --runs 20 --seed 7` plays four recorded custom tasks, five runs each, with one to three
kills per run. Each kill is of one of four kinds:

| Kind | What it does |
|---|---|
| `worker` | SIGKILL to the worker process |
| `sandbox` | `docker kill` of the sandbox container of the task |
| `temporal` | a restart of the Temporal server |
| `postgres` | a restart of Postgres |

This module draws the plan from the seed and from nothing else, so a plan is the same on every
machine and `coverage_problems` can say before anything starts whether a kind is hit too rarely.
It does not say when a kill lands. That follows the progress of a run that was not killed, and
the harness plans it (`tests.chaos.harness.plan_kills`), because it needs the activities of that
run.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

WORKER = "worker"
KILL_TYPES = (WORKER, "sandbox", "temporal", "postgres")
# The tasks with a recording, in the order the runs take them.
RECORDED_TASKS = ("calc-01", "calc-02", "calc-03", "inventory-01")
DEFAULT_RUNS = 20
DEFAULT_SEED = 7
MAX_KILLS = 3
# The story asks for each kind to be hit at least this often across the 20 runs.
MIN_HITS = 4


@dataclass(frozen=True)
class RunPlan:
    """One run of the suite: a task, the seed that aims its kills, and the kinds of its kills."""

    index: int  # from 1
    task_id: str
    seed: int  # for tests.chaos.harness.plan_kills
    kinds: tuple[str, ...]  # one for each kill, in the order they happen

    def __post_init__(self) -> None:
        if not 1 <= len(self.kinds) <= MAX_KILLS:
            raise ValueError(f"a run has 1 to {MAX_KILLS} kills, not {len(self.kinds)}")
        for kind in self.kinds:
            if kind not in KILL_TYPES:
                raise ValueError(f"cannot kill {kind!r}; the kinds are {', '.join(KILL_TYPES)}")


def plan_runs(runs: int, seed: int, tasks: Sequence[str] = RECORDED_TASKS) -> list[RunPlan]:
    """The plan of `runs` runs. Run n takes the tasks in turn, so each gets runs / len(tasks).

    A short suite is the start of a long one: `plan_runs(2, 7)` is the first two runs of
    `plan_runs(20, 7)`, because each draw reads the generator in the same order.
    """
    if runs < 1:
        raise ValueError(f"runs must be at least 1, not {runs}")
    if not tasks:
        raise ValueError("the suite needs at least 1 task")
    rng = random.Random(f"chaos-plan-{seed}")
    plans: list[RunPlan] = []
    for index in range(1, runs + 1):
        count = rng.randint(1, MAX_KILLS)
        kinds = tuple(rng.choice(KILL_TYPES) for _ in range(count))
        plans.append(
            RunPlan(
                index=index,
                task_id=tasks[(index - 1) % len(tasks)],
                seed=seed * 1000 + index,
                kinds=kinds,
            )
        )
    return plans


def hits_by_type(plans: Sequence[RunPlan]) -> dict[str, int]:
    """How many kills of each kind the plans hold, with a zero for a kind that none holds."""
    counted = Counter(kind for plan in plans for kind in plan.kinds)
    return {kind: counted[kind] for kind in KILL_TYPES}


def coverage_problems(plans: Sequence[RunPlan], minimum: int = MIN_HITS) -> list[str]:
    """The kinds that the plans hit fewer than `minimum` times, one line for each."""
    return [
        f"{kind} is hit {count} times, and the suite needs {minimum}"
        for kind, count in hits_by_type(plans).items()
        if count < minimum
    ]
