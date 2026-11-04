"""The parts of the chaos harness that need no stack: kill points, kill plans and the chain check.

The harness decides whether a chaos run counts as passed, so the rules it applies are tested
here without Docker or Temporal.
"""

import pytest
from tests.chaos import harness
from tests.chaos.harness import (
    BETWEEN,
    KINDS,
    STARTUP,
    CallRow,
    Run,
    chain_problems,
    kill_point,
    kills_in_run,
    plan_kills,
    survived,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

STAMP = "2026-11-04 08:00:00,000 INFO longhaul.durable.activity_log:"


def line(name: str, event: str, detail: str = "") -> str:
    return f"{STAMP} activity {name} {event}: task lh-t-1, attempt 1{detail}"


# ---------- where a worker was ----------
def test_a_started_activity_is_where_the_worker_was() -> None:
    log = "\n".join(
        [line("llm_turn", "started", ", turn 3"), line("llm_turn", "ended", ", turn 3")]
        + [line("tool_exec", "started", ", turn 3, edit_file")]
    )
    kind, last = kill_point(log)
    assert kind == "tool_exec"
    assert last == "activity tool_exec started: task lh-t-1, attempt 1, turn 3, edit_file"


@pytest.mark.parametrize("event", ["ended", "failed"])
def test_a_finished_activity_puts_the_worker_between_two(event: str) -> None:
    log = "\n".join([line("tool_exec", "started"), line("tool_exec", event)])
    assert kill_point(log)[0] == BETWEEN


def test_lines_that_are_not_activity_lines_do_not_move_the_kill_point() -> None:
    log = "\n".join(
        [
            line("llm_turn", "started", ", turn 3"),
            f"{STAMP} cleaning up task lh-t-1: cancelled",
            "Traceback (most recent call last):",
            "worker stopped",
        ]
    )
    assert kill_point(log)[0] == "llm_turn"


def test_a_worker_that_took_no_work_was_at_startup() -> None:
    assert kill_point("worker running on task queue q\n") == (STARTUP, "")


def test_the_kinds_the_story_names() -> None:
    assert KINDS == ("llm_turn", "tool_exec", BETWEEN)


# ---------- kill plans ----------
def test_a_seed_is_a_repeatable_plan() -> None:
    assert plan_kills(7, 3, 30.0) == plan_kills(7, 3, 30.0)
    assert plan_kills(7, 3, 30.0) != plan_kills(8, 3, 30.0)


def test_delays_stay_between_half_a_second_and_the_expected_duration() -> None:
    for seed in range(50):
        for kill in plan_kills(seed, 3, 20.0):
            assert 0.5 <= kill.delay_s <= 20.0 * harness.WINDOW_SHARE


def test_a_short_expected_duration_still_gives_a_window() -> None:
    for kill in plan_kills(1, 5, 0.1):
        assert 0.5 <= kill.delay_s <= 1.5


def test_the_first_kill_waits_for_the_first_model_turn_and_later_ones_for_finished_work() -> None:
    first, second, third = plan_kills(3, 3, 20.0)
    assert first.after is harness.FIRST_TURN
    assert second.after is third.after is harness.ANY_ENDED


def test_every_plan_has_the_count_it_was_given() -> None:
    assert [len(plan_kills(1, n, 20.0)) for n in (1, 2, 3)] == [1, 2, 3]


def test_a_full_chaos_run_has_one_to_three_kills_and_all_three_counts_occur() -> None:
    counts = [kills_in_run(seed) for seed in range(1, 21)]
    assert set(counts) == {1, 2, 3}
    assert kills_in_run(5) == kills_in_run(5)


# ---------- the chain of checkpoints ----------
def call(
    n: int, pre: str | None, post: str | None = None, name: str = "edit_file", attempt: int = 1
) -> CallRow:
    return CallRow(f"toolu_{n}", n, name, attempt, pre, post)


def test_a_line_of_checkpoints_has_no_problems() -> None:
    calls = [
        call(0, "base", None, "read_file"),
        call(1, "base", "a"),
        call(2, "a", None, "read_file"),
        call(3, "a", "b"),
        call(4, "b", "c", "run_tests", attempt=2),
    ]
    assert chain_problems(calls, "c") == []


def test_a_call_that_starts_from_the_wrong_checkpoint_is_a_problem() -> None:
    problems = chain_problems([call(1, "base", "a"), call(2, "base", "b")], "b")
    assert any("starts at" in p for p in problems), problems


def test_two_calls_that_save_from_the_same_checkpoint_are_a_fork() -> None:
    problems = chain_problems([call(1, "base", "a"), call(2, "base", "b")], "b")
    assert any("forks" in p for p in problems), problems


def test_two_calls_that_save_the_same_commit_are_a_problem() -> None:
    problems = chain_problems([call(1, "base", "a"), call(2, "a", "a")], "a")
    assert any("saved by two calls" in p for p in problems), problems


def test_a_chain_that_ends_short_of_the_head_is_a_problem() -> None:
    problems = chain_problems([call(1, "base", "a"), call(2, "a", "b")], "c")
    assert any("ends at b" in p for p in problems), problems


def test_no_rows_is_a_problem() -> None:
    assert chain_problems([], "a") == ["no tool_calls rows"]


# ---------- the verdict ----------
def make_run(**changes: object) -> Run:
    from longhaul.contracts import Outcome, OutcomeKind, Phase, TaskStatus

    values: dict[str, object] = {
        "task_id": "lh-t-1",
        "outcome": Outcome(kind=OutcomeKind.PR_OPENED, summary="done"),
        "status": TaskStatus(
            task_id="lh-t-1",
            phase=Phase.DONE,
            turn_index=2,
            cost_usd=0.0,
            last_checkpoint="b",
            plan_version=None,
        ),
        "diff": "the diff\n",
        "calls": [call(1, "base", "a"), call(2, "a", "b")],
        "first_parent": ["a", "b"],
        "ledger_attempts": {},
        "kills": [harness.KillPoint("tool_exec", "line", 3.0)],
        "planned_kills": 1,
        "work_s": 10.0,
    }
    return Run(**{**values, **changes})  # type: ignore[arg-type]


def test_a_run_that_matches_the_reference_survived() -> None:
    assert survived(make_run(), make_run(kills=[])) == []


def test_a_different_diff_is_a_failure() -> None:
    problems = survived(make_run(diff="another diff\n"), make_run())
    assert any("diff differs" in p for p in problems)


def test_a_run_that_did_not_finish_is_a_failure() -> None:
    from longhaul.contracts import Outcome, OutcomeKind

    outcome = Outcome(kind=OutcomeKind.ERROR, summary="sandbox lost")
    assert any(
        "did not end with finish" in p for p in survived(make_run(outcome=outcome), make_run())
    )


def test_a_run_whose_kill_never_happened_is_a_failure() -> None:
    problems = survived(make_run(kills=[]), make_run())
    assert any("0 of 1 planned kills" in p for p in problems)
    assert survived(make_run(kills=[]), make_run(), kills=0) == []


def test_a_fork_in_the_repo_is_a_failure_even_when_the_rows_are_clean() -> None:
    problems = survived(make_run(first_parent=["a", "x", "b"]), make_run())
    assert any("not the saved checkpoints" in p for p in problems)
