"""The full chaos suite: 20 runs that are killed, and a count of what they left behind (LH-904).

    uv run python eval/chaos/chaos.py --runs 20 --seed 7

It starts a Temporal and a Postgres of its own (a second compose project, see services.py), plays
four recorded custom tasks with the replay provider, and kills each run one to three times. A kill
is of the worker (SIGKILL), the sandbox (`docker kill`), the Temporal server or Postgres (a
restart). When to kill follows the progress of a run that was not killed, as in the harness.
Delivery goes to a FakeGitHub of the run's own, so the comments, label changes and pull requests
can be counted from its state.

For each run it records whether the task completed, whether the final diff is the diff of the run
that was not killed, the model calls that were billed again and what they cost, the time from each
kill to the next activity start (from Temporal's history), and the writes to GitHub against the
ones a task makes. `eval/results/chaos-<date>/summary.json` adds them up. The exit code says that
the suite ran. Whether the runs survived is in the summary.

    uv run python eval/chaos/chaos.py --record [--provider anthropic] [--tasks calc-01,calc-02]

records the model calls of the tasks into eval/chaos/recordings. The ones in the repo are scripted
runs, because no key is available where they were made. See eval/chaos/README.md.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

if __package__ in (None, ""):  # started as `python eval/chaos/chaos.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.chaos.harness import (  # noqa: E402
    CONFIG,
    Kill,
    KillPoint,
    Run,
    plan_kills,
    scratch_database,
    survived,
)

from eval import custom_tasks  # noqa: E402
from eval.chaos import audit, history, plan, results  # noqa: E402
from eval.chaos.results import KillResult, RunResult, SideEffects  # noqa: E402
from eval.chaos.services import PrivateServices  # noqa: E402
from eval.chaos.stack import ChaosStack, recording_env, replay_env  # noqa: E402
from eval.chaos.world import World  # noqa: E402
from eval.run import git_sha  # noqa: E402
from longhaul.contracts import OutcomeKind, TaskSpec  # noqa: E402
from longhaul.durable.client import connect  # noqa: E402
from longhaul.settings import load_config  # noqa: E402

ROOT = custom_tasks.ROOT
DEFAULT_RESULTS_DIR = ROOT / "eval" / "results"
RECORDINGS_DIR = ROOT / "eval" / "chaos" / "recordings"
SCRIPTS_DIR = ROOT / "tests" / "fixtures" / "scripts" / "eval"
SOURCE_FILE = "SOURCE"
PROVIDERS = ("scripted", "anthropic")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

# The kills of the full suite may land in the delivery of the pull request, the last part of a
# run. The audit of the writes to GitHub needs them there.
WINDOW_SHARE = 1.0
# How long the history of a task is waited for after a run, when the server may be coming back.
HISTORY_TIMEOUT_S = 120.0
# How much of a problem goes into a result file. A diff can be long.
PROBLEM_CHARS = 600


# ---------- the command line ----------
@dataclass(frozen=True)
class ChaosOptions:
    runs: int
    seed: int
    tasks: tuple[str, ...]
    results_dir: Path
    name: str | None
    record: bool
    provider: str


def positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a whole number above 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval/chaos/chaos.py",
        description="Kill runs of recorded tasks on a private Temporal and Postgres, and count "
        "what they leave behind.",
    )
    parser.add_argument("--runs", type=positive_int, default=plan.DEFAULT_RUNS)
    parser.add_argument("--seed", type=positive_int, default=plan.DEFAULT_SEED)
    parser.add_argument(
        "--tasks",
        default=",".join(plan.RECORDED_TASKS),
        help="the recorded tasks to take in turn, as ids separated by commas",
    )
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--name", help="the folder under the results folder (default chaos-DATE)")
    parser.add_argument(
        "--record",
        action="store_true",
        help="record the model calls of the tasks into eval/chaos/recordings and stop",
    )
    parser.add_argument(
        "--provider",
        choices=PROVIDERS,
        help="with --record: scripted (default) plays tests/fixtures/scripts/eval, anthropic "
        "asks the model and needs ANTHROPIC_API_KEY",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> ChaosOptions:
    """Read the command line. Every mistake ends with a usage error (exit code 2)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    tasks = tuple(part.strip() for part in args.tasks.split(",") if part.strip())
    unknown = [task for task in tasks if task not in plan.RECORDED_TASKS]
    if not tasks or unknown:
        parser.error(
            f"--tasks must name recorded tasks ({', '.join(plan.RECORDED_TASKS)}), "
            f"not {', '.join(unknown) or 'nothing'}"
        )
    if args.name is not None and (not _NAME.fullmatch(args.name) or ".." in args.name):
        parser.error(f"--name must be 1 to 64 letters, digits, '.', '_' or '-', not {args.name!r}")
    if args.provider is not None and not args.record:
        parser.error("--provider only goes with --record")
    return ChaosOptions(
        runs=args.runs,
        seed=args.seed,
        tasks=tasks,
        results_dir=args.results_dir,
        name=args.name,
        record=args.record,
        provider=args.provider or "scripted",
    )


def folder_name(day: date) -> str:
    return f"chaos-{day.isoformat()}"


def claim_folder(results_dir: Path, name: str) -> Path:
    """A new folder for a suite. One that exists is never used a second time."""
    folder = results_dir / name
    try:
        folder.mkdir(parents=True)
    except FileExistsError:
        raise ValueError(f"{folder} exists already. Use another --name") from None
    return folder


# ---------- one run ----------
@dataclass
class Observation:
    """What one run left behind, whether it finished or not."""

    spec: TaskSpec
    run: Run | None
    error: str | None
    kill_points: list[KillPoint]
    ledger_rows: int
    cost_usd: float
    side_effects: SideEffects
    starts: list[datetime]
    wall_time_s: float


async def activity_starts(stack: ChaosStack, task_id: str) -> list[datetime]:
    """The start times of every activity of the task, over every run of its chain."""
    settings = stack.settings

    async def read() -> list[datetime]:
        client = await connect(settings["temporal"], namespace=settings["namespace"])

        async def one(run_id: str | None) -> list[history.HistoryEvent]:
            handle = client.get_workflow_handle(task_id, run_id=run_id)
            return list((await handle.fetch_history()).events)

        return history.activity_starts(await history.chain_history(one))

    async with asyncio.timeout(HISTORY_TIMEOUT_S):
        return await stack.through_restarts(read)


def observe(
    task: custom_tasks.CustomTask,
    private: PrivateServices,
    provider_env: dict[str, str],
    kills: Sequence[Kill] = (),
) -> Observation:
    """Play the task once on the private services, with the kills, and read what it left."""
    began = time.monotonic()
    with (
        tempfile.TemporaryDirectory(prefix="lh-chaos-", ignore_cleanup_errors=True) as work,
        scratch_database(private.database) as database,
    ):
        world = World.open(Path(work) / "world", task)
        stack = ChaosStack(
            Path(work) / "stack", database, private=private, world=world, provider_env=provider_env
        )
        try:
            run: Run | None = None
            error: str | None = None
            try:
                run = stack.run(kills)
            except AssertionError as exc:  # Stack.run wraps every failure of a run in one
                error = str(exc)
            task_id = run.task_id if run else (stack.task_ids[-1] if stack.task_ids else None)
            rows, cost = stack.ledger_totals(task_id) if task_id else (0, 0.0)
            starts = asyncio.run(activity_starts(stack, task_id)) if task_id else []
            return Observation(
                spec=world.spec(task_id or "none"),
                run=run,
                error=error,
                kill_points=list(stack.kill_points),
                ledger_rows=rows,
                cost_usd=cost,
                side_effects=world.side_effects(),
                starts=starts,
                wall_time_s=time.monotonic() - began,
            )
        finally:
            stack.close()
            world.close()


def kill_results(
    run_plan: plan.RunPlan, seen: Observation, clock_offset_s: float
) -> list[KillResult]:
    """One result for each planned kill. A kill that never happened says so."""
    found: list[KillResult] = []
    for index, kind in enumerate(run_plan.kinds):
        point = seen.kill_points[index] if index < len(seen.kill_points) else None
        latency = None
        if point is not None and point.hit and point.struck_at is not None:
            latency = history.resume_latency_s(seen.starts, point.struck_at, clock_offset_s)
        found.append(
            KillResult(
                type=point.via if point else kind,
                landed=point is not None and point.hit,
                place=point.kind if point else None,
                struck_at=point.struck_at if point else None,
                resume_latency_s=latency,
            )
        )
    return found


def judge(
    run_plan: plan.RunPlan, seen: Observation, reference: Observation, clock_offset_s: float
) -> RunResult:
    """Compare a run with the same task run without a kill, and with what a task writes."""
    expected = audit.expected_side_effects(seen.spec)
    kills = kill_results(run_plan, seen, clock_offset_s)
    run = seen.run
    if run is None or reference.run is None:
        problems = [seen.error or "the run did not end"]
        completed, outcome, same_diff = False, "error", False
    else:
        problems = survived(run, reference.run, kills=0)
        completed = run.outcome.kind is OutcomeKind.PR_OPENED
        outcome, same_diff = run.outcome.kind.value, run.diff == reference.run.diff
    return RunResult(
        run=run_plan.index,
        task_id=run_plan.task_id,
        workflow_id=seen.spec.task_id,
        seed=run_plan.seed,
        kills_planned=list(run_plan.kinds),
        kills=kills,
        completed=completed,
        outcome=outcome,
        diff_equals_reference=same_diff,
        problems=[problem[:PROBLEM_CHARS] for problem in problems],
        ledger_rows=seen.ledger_rows,
        extra_ledger_rows=seen.ledger_rows - reference.ledger_rows,
        cost_usd=round(seen.cost_usd, 6),
        extra_cost_usd=round(seen.cost_usd - reference.cost_usd, 6),
        resume_latency_s=[k.resume_latency_s for k in kills if k.resume_latency_s is not None],
        side_effects=seen.side_effects,
        expected_side_effects=expected,
        duplicate_side_effects=audit.duplicates(seen.side_effects, expected),
        missing_side_effects=audit.missing(seen.side_effects, expected),
        wall_time_s=round(seen.wall_time_s, 1),
    )


def describe(result: RunResult) -> str:
    kinds = "+".join(result.kills_planned)
    landed = sum(k.landed for k in result.kills)
    latency = ", ".join(f"{v:.1f} s" for v in result.resume_latency_s) or "no resume"
    verdict = "survived" if result.survived else "DID NOT SURVIVE"
    return (
        f"run {result.run:02d} {result.task_id} [{kinds}]: {verdict}, {landed} of "
        f"{len(result.kills)} kills landed, resume {latency}, "
        f"{result.extra_ledger_rows} extra ledger rows, {result.duplicate_side_effects} "
        f"duplicate writes ({result.wall_time_s:.0f} s)"
    )


# ---------- the suite ----------
def recording_source(task_id: str) -> str:
    path = RECORDINGS_DIR / task_id / SOURCE_FILE
    return path.read_text(encoding="utf-8").strip() if path.is_file() else "unknown"


def check_recordings(tasks: Sequence[str]) -> None:
    for task_id in dict.fromkeys(tasks):
        if not any((RECORDINGS_DIR / task_id).glob("*.json")):
            raise ValueError(
                f"there is no recording for {task_id} in {RECORDINGS_DIR / task_id}. "
                "Make one with: uv run python eval/chaos/chaos.py --record"
            )


def task_named(task_id: str) -> custom_tasks.CustomTask:
    return next(task for task in custom_tasks.load_tasks() if task.id == task_id)


def play_reference(task_id: str, private: PrivateServices) -> Observation:
    """The run that is not killed. Every killed run of the task is compared with it."""
    seen = observe(task_named(task_id), private, replay_env(RECORDINGS_DIR / task_id))
    if seen.run is None:
        raise RuntimeError(f"the run of {task_id} without a kill failed:\n{seen.error}")
    problems = survived(seen.run, seen.run, kills=0)
    expected = audit.expected_side_effects(seen.spec)
    if seen.side_effects != expected:
        problems.append(f"it wrote {seen.side_effects}, and a task writes {expected}")
    if problems:
        raise RuntimeError(f"the run of {task_id} without a kill is not sound: {problems}")
    return seen


def play_run(
    run_plan: plan.RunPlan, reference: Observation, private: PrivateServices, offset_s: float
) -> tuple[RunResult, Observation]:
    assert reference.run is not None
    kills = plan_kills(
        run_plan.seed,
        len(run_plan.kinds),
        reference.run.activities,
        kinds=run_plan.kinds,
        window_share=WINDOW_SHARE,
    )
    seen = observe(
        task_named(run_plan.task_id), private, replay_env(RECORDINGS_DIR / run_plan.task_id), kills
    )
    return judge(run_plan, seen, reference, offset_s), seen


def run_suite(options: ChaosOptions) -> int:
    plans = plan.plan_runs(options.runs, options.seed, options.tasks)
    check_recordings([p.task_id for p in plans])
    problems = plan.coverage_problems(plans)
    if problems and options.runs >= plan.DEFAULT_RUNS:
        print("warning: " + "; ".join(problems), file=sys.stderr)
    name = options.name or folder_name(date.today())
    folder = claim_folder(options.results_dir, name)
    started_at = datetime.now(UTC)
    private = PrivateServices.start()
    print(
        f"private services: {private.project} (postgres {private.ports.postgres}, "
        f"temporal {private.ports.temporal})",
        flush=True,
    )
    found: list[RunResult] = []
    try:
        offset_s = private.clock_offset_s()
        references: dict[str, Observation] = {}
        for run_plan in plans:
            if run_plan.task_id not in references:
                references[run_plan.task_id] = play_reference(run_plan.task_id, private)
                print(f"reference {run_plan.task_id}: ok", flush=True)
            result, seen = play_run(run_plan, references[run_plan.task_id], private, offset_s)
            results.write_run(folder, result)
            if not result.survived and seen.run is not None:
                (folder / f"run-{result.run:02d}.log").write_text(
                    seen.run.report(), encoding="utf-8"
                )
            found.append(result)
            print(describe(result), flush=True)
    finally:
        private.remove()
    summary = results.summarize(
        found,
        name=name,
        seed=options.seed,
        git_sha=git_sha(),
        tasks=list(dict.fromkeys(p.task_id for p in plans)),
        recordings={t: recording_source(t) for t in dict.fromkeys(p.task_id for p in plans)},
        started_at=started_at,
        ended_at=datetime.now(UTC),
        clock_offset_s=offset_s,
        compose_project=private.project,
        temporal_heals=private.watchdog.heals,
    )
    results.write_summary(folder, summary)
    print(
        f"{summary.survived} of {summary.runs} runs survived, {summary.completed} completed, "
        f"{summary.duplicate_side_effects} duplicate writes, "
        f"{summary.extra_ledger_rows} extra ledger rows (${summary.extra_cost_usd:.4f}), "
        f"resume median {summary.resume_latency.median_s} s, "
        f"{summary.temporal_heals} restarts by the watchdog ({folder / results.SUMMARY_FILE})"
    )
    return 0


# ---------- recording ----------
def record_suite(options: ChaosOptions) -> int:
    """Record the model calls of the tasks, one unkilled run each, into eval/chaos/recordings."""
    provider_env: dict[str, str] = {}
    if options.provider == "anthropic":
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            print(
                "error: --provider anthropic needs ANTHROPIC_API_KEY in the environment",
                file=sys.stderr,
            )
            return 2
        provider_env["ANTHROPIC_API_KEY"] = key
    source = "scripted" if options.provider == "scripted" else load_config(CONFIG).models.coder
    private = PrivateServices.start()
    try:
        for task_id in options.tasks:
            with tempfile.TemporaryDirectory(
                prefix="lh-record-", ignore_cleanup_errors=True
            ) as scratch:
                script = SCRIPTS_DIR / f"{task_id}.yaml" if options.provider == "scripted" else None
                env = recording_env(Path(scratch), options.provider, script)
                seen = observe(task_named(task_id), private, {**env, **provider_env})
                if seen.run is None or seen.run.outcome.kind is not OutcomeKind.PR_OPENED:
                    print(
                        f"error: the run of {task_id} did not finish:\n{seen.error}",
                        file=sys.stderr,
                    )
                    return 1
                calls = save_recording(task_id, Path(scratch), source)
            print(f"recorded {task_id}: {calls} calls ({source})", flush=True)
    finally:
        private.remove()
    return 0


def save_recording(task_id: str, recorded: Path, source: str) -> int:
    """Replace the recording of a task. The first recording of a request is kept by the
    provider, so a folder that holds an older run must be emptied before."""
    folder = RECORDINGS_DIR / task_id
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.json"):
        old.unlink()
    calls = sorted(recorded.glob("*.json"))
    for call in calls:
        shutil.copy2(call, folder / call.name)
    (folder / SOURCE_FILE).write_text(source + "\n", encoding="utf-8")
    return len(calls)


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    try:
        return record_suite(options) if options.record else run_suite(options)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
