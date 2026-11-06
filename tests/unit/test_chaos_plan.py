"""The plan of the full chaos suite: which task, how many kills, and of what kind (LH-904).

The plan is drawn from the seed and nothing else, so `--runs 20 --seed 7` is the same twenty runs
on every machine. The story asks for every kind of kill to be hit at least four times across the
twenty, and `test_kill_type_coverage` is where that is asserted for the seed the suite uses.
"""

from collections import Counter

import pytest
from tests.chaos import harness

from eval.chaos import plan
from eval.chaos.plan import KILL_TYPES, RECORDED_TASKS, RunPlan, plan_runs

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

RUNS, SEED = 20, 7


def test_kill_type_coverage() -> None:
    plans = plan_runs(RUNS, SEED)

    hits = plan.hits_by_type(plans)

    assert set(hits) == set(KILL_TYPES)
    assert all(count >= plan.MIN_HITS == 4 for count in hits.values()), hits
    assert plan.coverage_problems(plans) == []


def test_the_suite_defaults_are_the_ones_of_the_story() -> None:
    assert (plan.DEFAULT_RUNS, plan.DEFAULT_SEED) == (RUNS, SEED)
    assert KILL_TYPES == ("worker", "sandbox", "temporal", "postgres")
    assert len(RECORDED_TASKS) == 4


def test_the_worker_kind_is_the_one_the_harness_kills_with() -> None:
    assert plan.KILL_TYPES[0] == harness.WORKER


def test_each_recorded_task_gets_five_of_the_twenty_runs() -> None:
    counts = Counter(p.task_id for p in plan_runs(RUNS, SEED))

    assert counts == {task: 5 for task in RECORDED_TASKS}


def test_every_run_has_one_to_three_kills_and_all_three_counts_occur() -> None:
    sizes = [len(p.kinds) for p in plan_runs(RUNS, SEED)]

    assert set(sizes) == {1, 2, 3}


def test_runs_are_numbered_from_one_and_have_seeds_of_their_own() -> None:
    plans = plan_runs(RUNS, SEED)

    assert [p.index for p in plans] == list(range(1, RUNS + 1))
    assert len({p.seed for p in plans}) == RUNS


def test_a_seed_is_a_repeatable_plan() -> None:
    assert plan_runs(RUNS, SEED) == plan_runs(RUNS, SEED)


def test_another_seed_is_another_plan() -> None:
    assert plan_runs(RUNS, SEED) != plan_runs(RUNS, SEED + 1)


def test_the_short_suite_is_the_start_of_the_long_one() -> None:
    # `--runs 2` plays the first two runs of `--runs 20`, so a short run checks a real one.
    assert plan_runs(2, SEED) == plan_runs(RUNS, SEED)[:2]


def test_a_short_suite_still_touches_more_than_one_task() -> None:
    assert {p.task_id for p in plan_runs(2, SEED)} == set(RECORDED_TASKS[:2])


def test_the_tasks_can_be_named() -> None:
    plans = plan_runs(4, SEED, tasks=["calc-01"])

    assert {p.task_id for p in plans} == {"calc-01"}


@pytest.mark.parametrize("runs", [0, -3])
def test_a_suite_needs_at_least_one_run(runs: int) -> None:
    with pytest.raises(ValueError, match="runs"):
        plan_runs(runs, SEED)


def test_a_suite_needs_a_task() -> None:
    with pytest.raises(ValueError, match="task"):
        plan_runs(3, SEED, tasks=[])


def test_a_missing_kind_is_a_coverage_problem() -> None:
    plans = [RunPlan(1, "calc-01", 1, ("worker", "sandbox", "temporal"))]

    problems = plan.coverage_problems(plans, minimum=1)

    assert problems == ["postgres is hit 0 times, and the suite needs 1"]


def test_a_kind_hit_too_rarely_is_a_coverage_problem() -> None:
    plans = [
        RunPlan(1, "calc-01", 1, ("worker", "worker", "worker")),
        RunPlan(2, "calc-02", 2, ("sandbox", "temporal", "postgres")),
    ]

    problems = plan.coverage_problems(plans, minimum=2)

    assert [p.split(" ")[0] for p in problems] == ["sandbox", "temporal", "postgres"]


def test_hits_count_every_kill_of_every_run() -> None:
    plans = [
        RunPlan(1, "calc-01", 1, ("worker", "worker")),
        RunPlan(2, "calc-02", 2, ("postgres",)),
    ]

    assert plan.hits_by_type(plans) == {"worker": 2, "sandbox": 0, "temporal": 0, "postgres": 1}


def test_a_kind_the_suite_cannot_strike_is_refused() -> None:
    with pytest.raises(ValueError, match="tape"):
        RunPlan(1, "calc-01", 1, ("tape",))


def test_a_run_has_between_one_and_three_kills() -> None:
    with pytest.raises(ValueError, match="1 to 3"):
        RunPlan(1, "calc-01", 1, ())
    with pytest.raises(ValueError, match="1 to 3"):
        RunPlan(1, "calc-01", 1, ("worker",) * 4)
