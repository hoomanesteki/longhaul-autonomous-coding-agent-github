"""Kill the worker at random points and show that the run still ends right (LH-209).

`longhaul worker` runs as a process. The test starts a task, sends SIGKILL to the worker, starts
it again, and then checks what the run left behind:

* the run ended with `finish`;
* its final diff equals the diff of a run that was never killed;
* its `tool_calls` rows are one line of checkpoints, from the first pre_sha to the last
  post_sha, with no fork (a call that was retried appears once, as its last attempt).

The scripted run is tests/fixtures/scripts/inventory_fix.yaml. It has a command that is not safe
to run twice: it appends a line to CHANGES.txt and then sleeps. A kill inside that call leaves
the line in the workspace, and the retried call must start from `pre_sha` (ADR-0006) or the line
is written twice and the diff differs. Remove `restore` from `ToolActivities._start_from` and
`test_kill_during_tool_exec_resumes` fails; that is the check that the test can fail.

Kill points. The three named tests aim a kill at one kind of place each: inside `tool_exec`,
inside `llm_turn`, and between two activities. Each asserts the place from the last log line of the
worker, so a kill that missed its aim fails the test instead of passing for the wrong reason. The
two seeded tests kill at a random point of the run's progress and report where it fell. The point
is drawn from the activities of the reference run (plan_kills): the kill waits for the k-th
activity of the worker to start and then pauses for no longer than that activity took in the
reference. It follows the worker's progress and not the clock, so a machine that is busier or
quieter than it was for the reference does not move a kill past the end of its run.

Markers. `chaos_lite` is the five runs of CI and of the gates. `chaos` alone is the full suite: 20
seeded runs with 1 to 3 kills each, answered by the replay provider, for Phase 9. It plays
eval/chaos/recordings/inventory when that holds a recording of a real model, and otherwise it
records a scripted run first, with the same code.

Run `make up` and `make sandbox-image` first. The workspace setup installs pytest from PyPI.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.chaos.harness import (
    BETWEEN,
    COMMAND_STARTED,
    EDIT_ENDED,
    ROOT,
    SCRIPT,
    SLOW_TURN_STARTED,
    Kill,
    Run,
    Stack,
    appended,
    kills_in_run,
    plan_kills,
    scratch_database,
    survived,
)

pytestmark = [pytest.mark.chaos, pytest.mark.phase2]

RECORDED = ROOT / "eval" / "chaos" / "recordings" / "inventory"
SEEDS = range(1, 21)
# The turn of inventory_fix.yaml that waits for the model, and the pause after its call starts.
SLOW_TURN = 3
INTO_THE_WAIT_S = 1.0


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    with scratch_database() as database:
        run = Stack(tmp_path, database)
        try:
            yield run
        finally:
            run.close()


def check(run: Run, reference: Run) -> None:
    problems = survived(run, reference)
    assert not problems, "\n".join(problems) + "\n" + run.report()


# ---------- the run that is never killed ----------
@pytest.fixture(scope="module")
def reference(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Run]:
    """The run every killed run is compared with: the same script, and no kill."""
    with scratch_database() as database:
        stack = Stack(tmp_path_factory.mktemp("reference"), database)
        try:
            run = stack.run()
        finally:
            stack.close()
    assert not run.kills
    problems = survived(run, run, kills=0)
    assert not problems, "\n".join(problems) + "\n" + run.report()
    files = [line for line in run.diff.splitlines() if line.startswith("diff --git")]
    assert len(files) == 5, (
        run.diff
    )  # errors.py, store.py, __init__.py, test_errors.py, CHANGES.txt
    assert run.diff.count("+remove() rejects an overdraft") == 1
    assert run.activities, "the reference worker logged no activity, so there is nothing to aim at"
    yield run


# ---------- chaos_lite: one kill each, five runs ----------
@pytest.mark.chaos_lite
def test_kill_during_tool_exec_resumes(stack: Stack, reference: Run) -> None:
    # The kill comes after the command wrote its line and before the call ended.
    run = stack.run([Kill(after=COMMAND_STARTED, until=appended(), note="inside run_command")])

    check(run, reference)
    (point,) = run.kills
    assert point.kind == "tool_exec", point.line
    (command,) = [c for c in run.calls if c.name == "run_command"]
    assert command.attempt == 2, "Temporal did not run the call again"


@pytest.mark.chaos_lite
def test_kill_during_llm_turn_resumes(stack: Stack, reference: Run) -> None:
    run = stack.run(
        [Kill(after=SLOW_TURN_STARTED, delay_s=INTO_THE_WAIT_S, note="inside the model wait")]
    )

    check(run, reference)
    (point,) = run.kills
    assert point.kind == "llm_turn", point.line
    # The first attempt was killed before the call returned, so it was never billed. The second
    # attempt sent the same request again and is the one on the ledger (plan section 4.1, F1).
    attempts = run.ledger_attempts[SLOW_TURN]
    assert 2 in attempts and 1 not in attempts, attempts


@pytest.mark.chaos_lite
def test_kill_between_activities(stack: Stack, reference: Run) -> None:
    run = stack.run([Kill(after=EDIT_ENDED, note="right after an edit")])

    check(run, reference)
    (point,) = run.kills
    assert point.kind == BETWEEN, point.line


@pytest.mark.chaos_lite
# Each seed picks the activity of the reference to wait for, and a pause shorter than that activity
# took there. The test prints which activity that was and where the kill fell (run it with -s). The
# aimed tests above cover the places that a random draw rarely hits.
@pytest.mark.parametrize("seed", [9, 19])
def test_a_kill_at_a_random_moment_resumes(stack: Stack, reference: Run, seed: int) -> None:
    (kill,) = plan = plan_kills(seed, 1, reference.activities)
    run = stack.run(plan)

    check(run, reference)
    (point,) = run.kills
    print(
        f"{kill.note}, then {kill.delay_s} s: "
        f"killed in {point.kind} at {point.after_s:.1f} s ({point.line})"
    )


# ---------- chaos: the full suite, answered by the replay provider ----------
@pytest.fixture(scope="module")
def recordings(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder of recorded model calls for the inventory fix.

    That is the recording of a real model when the repo has one, and otherwise a scripted run
    recorded now, through the same worker and the same provider code.
    """
    if any(RECORDED.glob("*.json")):
        return RECORDED
    folder = tmp_path_factory.mktemp("recordings")
    env = {
        "LONGHAUL_PROVIDER": "scripted",
        "LONGHAUL_SCRIPT": str(SCRIPT),
        "LONGHAUL_RECORD": "1",
        "LONGHAUL_RECORDINGS_DIR": str(folder),
    }
    with scratch_database() as database:
        stack = Stack(tmp_path_factory.mktemp("record"), database, worker_env=env, script=None)
        try:
            stack.run()
        finally:
            stack.close()
    assert any(folder.glob("*.json")), "the worker recorded nothing"
    return folder


def replay_stack(root: Path, database: str, recordings: Path) -> Stack:
    env = {"LONGHAUL_PROVIDER": "replay", "LONGHAUL_RECORDINGS_DIR": str(recordings)}
    # Every four turns the run hands over to a new one (Continue-As-New), so the kills also fall
    # in the hand-over: the bundle, the event and the first activity of the next run.
    return Stack(root, database, worker_env=env, script=None, continue_as_new_every=4)


@pytest.fixture(scope="module")
def replay_reference(recordings: Path, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Run]:
    with scratch_database() as database:
        stack = replay_stack(tmp_path_factory.mktemp("replay-reference"), database, recordings)
        try:
            run = stack.run()
        finally:
            stack.close()
    problems = survived(run, run, kills=0)
    assert not problems, "\n".join(problems) + "\n" + run.report()
    yield run


@pytest.mark.parametrize("seed", SEEDS)
def test_replayed_run_survives_one_to_three_kills(
    seed: int, recordings: Path, replay_reference: Run, tmp_path: Path
) -> None:
    kills = kills_in_run(seed)
    with scratch_database() as database:
        stack = replay_stack(tmp_path, database, recordings)
        try:
            run = stack.run(plan_kills(seed, kills, replay_reference.activities))
        finally:
            stack.close()

    problems = survived(run, replay_reference)
    assert not problems, "\n".join(problems) + "\n" + run.report()
    where = ", ".join(f"{k.kind} at {k.after_s:.1f} s" for k in run.kills)
    print(f"seed {seed}: {len(run.kills)} of {kills} kills ({where})")
