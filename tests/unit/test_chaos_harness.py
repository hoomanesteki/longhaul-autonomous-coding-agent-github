"""The parts of the chaos harness that need no stack: kill points, kill plans and the chain check.

The harness decides whether a chaos run counts as passed, so the rules it applies are tested
here without Docker or Temporal. The kill that follows a worker's progress is tested against a
real process and a log file that grows on a timer, at the pace of the reference and at half and
twice that pace.
"""

import contextlib
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.chaos import harness
from tests.chaos.harness import (
    ANY_ENDED,
    ANY_STARTED,
    BETWEEN,
    KINDS,
    STARTUP,
    WINDOW_SHARE,
    Activity,
    CallRow,
    Kill,
    Run,
    Stack,
    Worker,
    activities_of,
    chain_problems,
    kill_point,
    kills_in_run,
    plan_kills,
    survived,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

STAMP = "2026-11-04 08:00:00,000 INFO longhaul.durable.activity_log:"
BEGAN = datetime(2026, 11, 4, 8, 0, 0)


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


# ---------- a reference run, and workers that follow it ----------
# The reference runs at this pace. A worker that is twice as fast or twice as slow keeps its
# activities in the same order and scales every time.
UNIT_S = 0.05
FIRST_TURN_NTH = 2  # prepare_workspace is the first activity, the first model turn the second


def timed(when: datetime, name: str, event: str, detail: str = "") -> str:
    stamp = f"{when:%Y-%m-%d %H:%M:%S},{when.microsecond // 1000:03d}"
    where = f"task lh-t-1, attempt 1{detail}"
    return f"{stamp} INFO longhaul.durable.activity_log: activity {name} {event}: {where}"


def run_events(unit_s: float) -> list[tuple[float, str]]:
    """The log of a run as (seconds from its start, line).

    The run prepares its workspace (3 units), then makes ten turns of a model call (2 units) and
    an edit (1 unit). That is 21 activities, one after the other.
    """
    steps = [("prepare_workspace", "", 3)]
    for turn in range(10):
        steps.append(("llm_turn", f", turn {turn}", 2))
        steps.append(("tool_exec", f", turn {turn}, edit_file", 1))
    events: list[tuple[float, str]] = []
    clock = 0.0
    for name, detail, units in steps:
        for event, offset in (("started", 0.0), ("ended", units * unit_s)):
            at = clock + offset
            events.append((at, timed(BEGAN + timedelta(seconds=at), name, event, detail)))
        clock += units * unit_s
    return events


def log_of(events: list[tuple[float, str]]) -> str:
    return "".join(f"{text}\n" for _, text in events)


REFERENCE = activities_of(log_of(run_events(UNIT_S)))
# The first kill may wait for the k-th started activity: from the first model turn to the last
# one that leaves the final share of the run to do.
NTH_CHOICES = range(FIRST_TURN_NTH, int(WINDOW_SHARE * len(REFERENCE)) + 1)


# ---------- the activities of a log ----------
def test_a_log_gives_its_activities_in_the_order_they_started() -> None:
    names = [a.name for a in REFERENCE]
    assert names == ["prepare_workspace"] + ["llm_turn", "tool_exec"] * 10


def test_an_activity_took_the_time_between_its_started_and_ended_lines() -> None:
    assert REFERENCE[0].took_s == pytest.approx(3 * UNIT_S, abs=0.002)
    assert REFERENCE[1].took_s == pytest.approx(2 * UNIT_S, abs=0.002)
    assert REFERENCE[2].took_s == pytest.approx(UNIT_S, abs=0.002)


def test_an_activity_keeps_its_started_line_without_the_time() -> None:
    assert REFERENCE[2].line == (
        "activity tool_exec started: task lh-t-1, attempt 1, turn 0, edit_file"
    )


def test_a_failed_activity_took_the_time_until_it_failed() -> None:
    log = "\n".join(
        [
            timed(BEGAN, "llm_turn", "started", ", turn 3"),
            timed(BEGAN + timedelta(seconds=1.5), "llm_turn", "failed", ", turn 3 (ProviderError)"),
        ]
    )
    (only,) = activities_of(log)
    assert only.took_s == pytest.approx(1.5, abs=0.002)


def test_an_activity_that_never_ended_took_no_time() -> None:
    (only,) = activities_of(timed(BEGAN, "tool_exec", "started", ", turn 8, run_command"))
    assert only.took_s == 0.0


def test_lines_that_are_not_activity_lines_are_not_activities() -> None:
    noisy = [text for _, text in run_events(UNIT_S)]
    noisy.insert(3, "worker running on task queue q")
    noisy.insert(8, f"{STAMP} cleaning up task lh-t-1: cancelled")
    assert activities_of("\n".join(noisy)) == REFERENCE


# ---------- kill plans ----------
def test_a_seed_is_a_repeatable_plan() -> None:
    assert plan_kills(7, 3, REFERENCE) == plan_kills(7, 3, REFERENCE)


def test_different_seeds_make_different_plans() -> None:
    plans = {tuple(plan_kills(seed, 3, REFERENCE)) for seed in range(1, 21)}
    assert len(plans) == 20


def test_a_plan_depends_only_on_the_seed_and_the_activities_of_the_reference() -> None:
    # The same activities from another run: other times, and lines of other kinds in between.
    later = [(at, text.replace("08:00:", "09:30:")) for at, text in run_events(UNIT_S)]
    log = log_of(later).replace("\n", "\nworker running on task queue q\n", 4)
    assert plan_kills(5, 3, activities_of(log)) == plan_kills(5, 3, REFERENCE)


def test_a_slower_reference_changes_the_pauses_and_not_the_activity_the_kill_waits_for() -> None:
    slower = activities_of(log_of(run_events(UNIT_S * 4)))
    for seed in range(1, 21):
        (fast,) = plan_kills(seed, 1, REFERENCE)
        (slow,) = plan_kills(seed, 1, slower)
        assert fast.nth == slow.nth
        assert slow.delay_s >= fast.delay_s


def test_the_first_kill_waits_for_a_started_activity_after_the_first_model_turn() -> None:
    for seed in range(1, 201):
        (kill,) = plan_kills(seed, 1, REFERENCE)
        assert kill.after is ANY_STARTED
        assert kill.nth in NTH_CHOICES, (seed, kill.nth)


def test_the_first_kill_can_wait_for_every_activity_in_its_window() -> None:
    drawn = {plan_kills(seed, 1, REFERENCE)[0].nth for seed in range(1, 401)}
    assert drawn == set(NTH_CHOICES)


def test_the_last_share_of_the_activities_is_left_to_run_after_the_kill() -> None:
    assert max(NTH_CHOICES) < len(REFERENCE)
    assert len(REFERENCE) - max(NTH_CHOICES) >= (1 - WINDOW_SHARE) * len(REFERENCE) - 1


def test_the_pause_of_the_first_kill_is_no_longer_than_its_activity_took() -> None:
    for seed in range(1, 201):
        (kill,) = plan_kills(seed, 1, REFERENCE)
        assert 0 <= kill.delay_s <= REFERENCE[kill.nth - 1].took_s


def test_a_later_kill_waits_for_the_new_worker_to_finish_an_activity() -> None:
    first, second, third = plan_kills(3, 3, REFERENCE)
    assert first.after is ANY_STARTED
    assert second.after is third.after is ANY_ENDED
    assert second.nth == third.nth == 1


def test_the_pause_of_a_later_kill_is_no_longer_than_the_longest_activity() -> None:
    longest = max(a.took_s for a in REFERENCE)
    for seed in range(1, 51):
        for kill in plan_kills(seed, 3, REFERENCE)[1:]:
            assert 0 <= kill.delay_s <= longest


def test_every_plan_has_the_count_it_was_given() -> None:
    assert [len(plan_kills(1, n, REFERENCE)) for n in (1, 2, 3)] == [1, 2, 3]


def test_a_reference_with_one_model_turn_still_gives_a_kill() -> None:
    only = [Activity("llm_turn", "activity llm_turn started: task t, attempt 1, turn 0", 0.5)]
    (kill,) = plan_kills(1, 1, only)
    assert kill.nth == 1


def test_a_reference_without_a_model_turn_cannot_be_aimed_at() -> None:
    with pytest.raises(ValueError, match="model turn"):
        plan_kills(1, 1, [a for a in REFERENCE if a.name != "llm_turn"])
    with pytest.raises(ValueError, match="model turn"):
        plan_kills(1, 1, [])


def test_a_kill_waits_for_at_least_the_first_match() -> None:
    with pytest.raises(ValueError, match="nth"):
        Kill(after=ANY_STARTED, nth=0)


# ---------- a kill follows the progress of its worker ----------
@contextlib.contextmanager
def worker_logging(
    folder: Path, events: list[tuple[float, str]]
) -> Iterator[tuple[Worker, threading.Event]]:
    """A worker that is a real process, with a log that gains each line at its time offset.

    The event is set when the last line is written. The process only sleeps: the kill goes to a
    pid and reads the log, and that is all the kill does.
    """
    log = folder / "worker-1.log"
    log.write_text("worker running on task queue q\n", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    finished, abandon = threading.Event(), threading.Event()

    def write() -> None:
        began = time.monotonic()
        with log.open("a", encoding="utf-8") as out:
            for at, text in events:
                if abandon.wait(max(0.0, began + at - time.monotonic())):
                    return
                out.write(text + "\n")
                out.flush()
        finished.set()

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        yield Worker(proc, log), finished
    finally:
        abandon.set()
        writer.join()
        proc.kill()
        proc.wait()


def kill_it(
    kill: Kill, worker: Worker, stop: threading.Event | None = None
) -> harness.KillPoint | None:
    stack = Stack(worker.log.parent / "stack", "postgresql://unused")
    return stack._kill_one(kill, worker, "lh-t-1", time.monotonic(), stop or threading.Event())


@pytest.mark.parametrize("pace", [0.5, 2.0], ids=["twice as fast", "twice as slow"])
@pytest.mark.parametrize("seed", [3, 19])
def test_a_kill_meets_a_run_that_is_faster_or_slower_than_the_reference(
    tmp_path: Path, seed: int, pace: float
) -> None:
    (kill,) = plan_kills(seed, 1, REFERENCE)

    with worker_logging(tmp_path, run_events(UNIT_S * pace)) as (worker, finished):
        point = kill_it(kill, worker)
        ended_first = finished.is_set()
        started = len(ANY_STARTED.findall(worker.text()))

    assert point is not None
    assert not ended_first, "the log had ended before the kill"
    assert started >= kill.nth
    assert worker.proc.poll() is not None, "the worker is still running"
    assert point.kind != STARTUP


@pytest.mark.parametrize(("lines", "killed"), [(2, False), (3, True)])
def test_a_kill_waits_for_the_nth_match_in_the_log_of_its_worker(
    tmp_path: Path, lines: int, killed: bool
) -> None:
    events = [(0.0, timed(BEGAN, "llm_turn", "started", f", turn {n}")) for n in range(lines)]
    # A kill that is never due waits until the test says there is nothing left to kill.
    stopped = threading.Event()
    giving_up = threading.Timer(0.5, stopped.set)
    giving_up.start()
    try:
        with worker_logging(tmp_path, events) as (worker, _):
            point = kill_it(Kill(after=ANY_STARTED, nth=3), worker, stopped)
            assert (point is not None) is killed
            assert (worker.proc.poll() is not None) is killed
    finally:
        giving_up.cancel()


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
        "activities": REFERENCE,
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


def test_a_json_log_line_reads_as_the_plain_form() -> None:
    line = (
        '{"timestamp": "2026-11-04T19:42:01.123456Z", "level": "info", '
        '"logger": "longhaul.durable.activity_log", '
        '"event": "activity llm_turn started: task lh-t, attempt 1, turn 3", "task_id": "lh-t"}\n'
    )
    plain = harness.plain_line(line)
    assert plain == (
        "2026-11-04 19:42:01,123 INFO longhaul.durable.activity_log: "
        "activity llm_turn started: task lh-t, attempt 1, turn 3\n"
    )
    assert harness.stamp(plain) is not None
    assert harness.SLOW_TURN_STARTED.search(plain)


def test_a_line_that_is_not_json_is_kept() -> None:
    line = "2026-11-04T19:42:01Z WARN temporal_sdk_core: something\n"
    assert harness.plain_line(line) == line
