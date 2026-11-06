"""The options, the task spec and the watch loop of eval/run.py (LH-901).

No Temporal, Docker or database here. The watch loop gets a fake task and a fake clock, so the
one-minute pause timeout costs no time. The runner against the real stack is in
tests/integration/test_eval_runner.py.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.service import RPCError, RPCStatusCode

from eval import custom_tasks, results, run
from eval.grade_custom import GradeResult
from eval.results import TaskResult, Tokens
from eval.run import RunOptions
from longhaul.checkpoints.git_store import CheckpointError, check_task_id
from longhaul.contracts import Outcome, OutcomeKind, Phase, TaskStatus
from longhaul.settings import load_config

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
CONFIG = load_config(ROOT / "config" / "longhaul.example.yaml")
BASE_ARGS = ["--set", "custom", "--config", "full", "--run-id", "try1"]


def first_task() -> custom_tasks.CustomTask:
    return custom_tasks.load_tasks()[0]


def refuses(capsys: pytest.CaptureFixture[str], argv: list[str]) -> str:
    with pytest.raises(SystemExit) as stopped:
        run.parse_args(argv)
    assert stopped.value.code == 2
    return capsys.readouterr().err


# ---------- options ----------
def test_the_defaults_are_a_single_task_at_a_time_with_the_one_minute_pause_timeout() -> None:
    options = run.parse_args(BASE_ARGS)
    assert options == RunOptions(task_set="custom", config="full", run_id="try1")
    assert options.concurrency == 1
    assert options.limit is None
    assert options.pause_timeout_s == 60.0
    assert options.results_dir == ROOT / "eval" / "results"


def test_every_flag_of_the_plan_is_accepted(tmp_path: Path) -> None:
    script = tmp_path / "fix.yaml"
    script.write_text("steps: []\n", encoding="utf-8")
    options = run.parse_args(
        [
            *BASE_ARGS,
            "--model",
            "claude-sonnet-5-5",
            "--limit",
            "2",
            "--concurrency",
            "3",
            "--memory",
            "off",
            "--context-mode",
            "no_compaction",
            "--provider",
            "scripted",
            "--script",
            str(script),
            "--results-dir",
            str(tmp_path),
        ]
    )
    assert options.model == "claude-sonnet-5-5"
    assert (options.limit, options.concurrency) == (2, 3)
    assert (options.memory, options.context_mode) == ("off", "no_compaction")
    assert (options.provider, options.script, options.results_dir) == ("scripted", script, tmp_path)


def test_the_swebench_set_is_accepted() -> None:
    options = run.parse_args(["--set", "swebench", "--config", "full", "--run-id", "try1"])

    assert options.task_set == "swebench"
    assert options.config == "full"


@pytest.mark.parametrize("config", ["naive", "single_shot"])
def test_the_baseline_configs_are_not_there_yet(
    capsys: pytest.CaptureFixture[str], config: str
) -> None:
    err = refuses(capsys, ["--set", "custom", "--config", config, "--run-id", "try1"])
    assert config in err
    assert "LH-903" in err


@pytest.mark.parametrize(
    "flags",
    [
        ["--limit", "0"],
        ["--concurrency", "0"],
        ["--memory", "maybe"],
        ["--context-mode", "everything"],
        ["--provider", "carrier-pigeon"],
        ["--pause-timeout-s", "0"],
    ],
)
def test_a_bad_value_stops_with_a_usage_error(
    capsys: pytest.CaptureFixture[str], flags: list[str]
) -> None:
    assert refuses(capsys, [*BASE_ARGS, *flags])


@pytest.mark.parametrize("run_id", ["", "../x", "a b", "-x", "x" * 80])
def test_a_run_id_must_be_a_safe_short_name(
    capsys: pytest.CaptureFixture[str], run_id: str
) -> None:
    assert "run-id" in refuses(capsys, ["--set", "custom", "--config", "full", "--run-id", run_id])


def test_the_run_id_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    assert "run-id" in refuses(capsys, ["--set", "custom", "--config", "full"])


def test_the_scripted_provider_needs_a_script(capsys: pytest.CaptureFixture[str]) -> None:
    assert "--script" in refuses(capsys, [*BASE_ARGS, "--provider", "scripted"])


def test_a_script_does_not_go_with_another_provider(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    script = tmp_path / "fix.yaml"
    script.write_text("steps: []\n", encoding="utf-8")
    err = refuses(capsys, [*BASE_ARGS, "--provider", "anthropic", "--script", str(script)])
    assert "scripted" in err


def test_a_script_alone_means_the_scripted_provider(tmp_path: Path) -> None:
    script = tmp_path / "fix.yaml"
    script.write_text("steps: []\n", encoding="utf-8")
    assert run.parse_args([*BASE_ARGS, "--script", str(script)]).provider == "scripted"


def test_a_run_with_a_script_is_scripted(tmp_path: Path) -> None:
    script = tmp_path / "fix.yaml"
    script.write_text("steps: []\n", encoding="utf-8")
    options = run.parse_args([*BASE_ARGS, "--script", str(script)])
    assert (options.provider, options.scripted) == ("scripted", True)


def test_a_run_without_a_script_is_not_scripted() -> None:
    assert run.parse_args(BASE_ARGS).scripted is False
    options = run.parse_args([*BASE_ARGS, "--provider", "anthropic"])
    assert (options.provider, options.scripted) == ("anthropic", False)


def test_a_script_that_does_not_exist_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert "--script" in refuses(capsys, [*BASE_ARGS, "--script", "/nowhere/fix.yaml"])


# ---------- tasks and scripts ----------
def test_limit_keeps_the_first_tasks_in_file_order() -> None:
    options = RunOptions(task_set="custom", config="full", run_id="try1", limit=2)
    assert [t.id for t in run.select_tasks(options)] == ["calc-01", "calc-02"]


def test_without_a_limit_every_task_runs() -> None:
    options = RunOptions(task_set="custom", config="full", run_id="try1")
    assert len(run.select_tasks(options)) == 15


def test_one_script_file_serves_every_task(tmp_path: Path) -> None:
    script = tmp_path / "fix.yaml"
    options = RunOptions(task_set="custom", config="full", run_id="r", script=script)
    assert run.script_for(options, first_task()) == script


def test_a_script_folder_holds_one_script_per_task(tmp_path: Path) -> None:
    (tmp_path / "calc-01.yaml").write_text("steps: []\n", encoding="utf-8")
    options = RunOptions(task_set="custom", config="full", run_id="r", script=tmp_path)
    assert run.script_for(options, first_task()) == tmp_path / "calc-01.yaml"


def test_a_script_folder_without_the_script_of_a_task_is_refused(tmp_path: Path) -> None:
    options = RunOptions(task_set="custom", config="full", run_id="r", script=tmp_path)
    with pytest.raises(ValueError, match=r"calc-01\.yaml"):
        run.script_for(options, first_task())


def test_without_a_script_the_worker_answers_with_its_own_provider() -> None:
    options = RunOptions(task_set="custom", config="full", run_id="r")
    assert run.script_for(options, first_task()) is None


def test_the_workflow_id_names_the_run_and_the_task() -> None:
    workflow_id = run.workflow_id_for("try1", first_task())
    assert workflow_id == "lh-eval-try1-calc-01"
    assert check_task_id(workflow_id) == workflow_id


def test_a_task_runs_in_local_mode_with_the_plan_gate_off() -> None:
    task = first_task()
    spec = run.build_spec(task, "lh-eval-try1-calc-01")
    assert spec.task_id == "lh-eval-try1-calc-01"
    assert spec.repo == f"local:{custom_tasks.REPOS_DIR / 'calc'}"
    assert spec.settings.repo == spec.repo
    assert spec.settings.plan_approval_required is False
    assert (spec.title, spec.body) == (task.issue_title, task.issue_body)
    assert spec.issue_number is None


def test_a_task_gets_the_caps_of_the_fairness_rules() -> None:
    # Plan 8.4: task_usd 2 on the custom set, task_turns 150, task_wall_minutes 120.
    budgets = run.build_spec(first_task(), "lh-eval-try1-calc-01").settings.budgets
    assert (budgets.task_usd, budgets.task_turns, budgets.task_wall_minutes) == (2.0, 150, 120)


def test_the_repo_of_a_task_is_checked_before_it_is_started() -> None:
    run.check_tasks(run.select_tasks(RunOptions(task_set="custom", config="full", run_id="r")))


# ---------- the model ----------
def test_the_model_defaults_to_the_one_the_worker_is_configured_with() -> None:
    options = RunOptions(task_set="custom", config="full", run_id="r")
    assert run.resolve_model(options, CONFIG) == CONFIG.models.coder


def test_a_model_the_config_names_is_accepted() -> None:
    options = RunOptions(task_set="custom", config="full", run_id="r", model=CONFIG.models.coder)
    assert run.resolve_model(options, CONFIG) == CONFIG.models.coder


def test_a_model_the_config_does_not_name_is_refused() -> None:
    # The worker picks the model from its config, so a result file must not claim another one.
    options = RunOptions(task_set="custom", config="full", run_id="r", model="claude-haiku-5-5")
    with pytest.raises(ValueError, match="LONGHAUL_CONFIG"):
        run.resolve_model(options, CONFIG)


# ---------- the run folder ----------
def test_a_new_run_gets_its_own_folder(tmp_path: Path) -> None:
    folder = run.claim_run_dir(tmp_path, "try1")
    assert folder == tmp_path / "try1"
    assert folder.is_dir()


def test_a_run_id_with_results_is_not_used_twice(tmp_path: Path) -> None:
    folder = run.claim_run_dir(tmp_path, "try1")
    (folder / "calc-01.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="try1"):
        run.claim_run_dir(tmp_path, "try1")


def test_a_run_folder_without_results_is_not_used_again_either(tmp_path: Path) -> None:
    # A run that was interrupted leaves such a folder, and it has started workflows under this
    # run id. Temporal refuses a second start of those ids, so the run id cannot work again.
    run.claim_run_dir(tmp_path, "try1")
    with pytest.raises(ValueError, match="try1"):
        run.claim_run_dir(tmp_path, "try1")


# ---------- workflow ids of a run ----------
class FakeServer:
    """The part of the Temporal client that get_task uses, with some workflows already there."""

    def __init__(self, existing: set[str], error: RPCStatusCode | None = None) -> None:
        self.existing = existing
        self.error = error
        self.asked: list[str] = []

    def get_workflow_handle_for(self, _run: object, workflow_id: str) -> SimpleNamespace:
        async def describe() -> object:
            self.asked.append(workflow_id)
            if self.error is not None:
                raise RPCError("the server failed", self.error, b"")
            if workflow_id not in self.existing:
                raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
            return object()

        return SimpleNamespace(describe=describe)


async def test_workflow_ids_that_nobody_used_are_free() -> None:
    server = FakeServer(set())
    await run.check_workflow_ids_free(server, ["lh-eval-r-calc-01", "lh-eval-r-calc-02"])  # type: ignore[arg-type]
    assert server.asked == ["lh-eval-r-calc-01", "lh-eval-r-calc-02"]


async def test_a_workflow_id_that_exists_already_stops_the_run_before_any_start() -> None:
    server = FakeServer({"lh-eval-r-calc-02"})
    with pytest.raises(ValueError, match=r"lh-eval-r-calc-02.*run id"):
        await run.check_workflow_ids_free(server, ["lh-eval-r-calc-01", "lh-eval-r-calc-02"])  # type: ignore[arg-type]


async def test_a_server_that_cannot_answer_is_not_taken_for_a_free_id() -> None:
    server = FakeServer(set(), error=RPCStatusCode.UNAVAILABLE)
    with pytest.raises(ConnectionError, match="lh-eval-r-calc-01"):
        await run.check_workflow_ids_free(server, ["lh-eval-r-calc-01"])  # type: ignore[arg-type]


# ---------- watching a task ----------
class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeTask:
    """A task that follows a timeline of phases, and stops when it is cancelled.

    `phases` is a list of (time, phase): the phase from that time on. `ends_at` is when the
    task finishes by itself. `stops_after` is how long it takes to stop after a cancel, or None
    for a task that ignores the cancel.
    """

    def __init__(
        self,
        clock: FakeClock,
        phases: list[tuple[float, Phase | None]],
        *,
        ends_at: float | None = None,
        stops_after: float | None = 2.0,
    ) -> None:
        self.clock = clock
        self.phases = phases
        self.ends_at = ends_at
        self.stops_after = stops_after
        self.cancelled_at: float | None = None
        self.cancel_reason: str | None = None
        self.terminated_at: float | None = None

    def closed_at(self) -> float | None:
        stops = []
        if self.ends_at is not None:
            stops.append(self.ends_at)
        if self.cancelled_at is not None and self.stops_after is not None:
            stops.append(self.cancelled_at + self.stops_after)
        if self.terminated_at is not None:
            stops.append(self.terminated_at)
        return min(stops) if stops else None

    async def is_open(self) -> bool:
        closes = self.closed_at()
        return closes is None or self.clock.now < closes

    async def status(self) -> TaskStatus | None:
        phase = [p for t, p in self.phases if t <= self.clock.now][-1]
        if phase is None:
            return None  # no worker answered
        return TaskStatus(
            task_id="lh-eval-try1-calc-01",
            phase=phase,
            turn_index=3,
            cost_usd=0.0,
            last_checkpoint=None,
            plan_version=None,
        )

    async def cancel(self, reason: str) -> None:
        self.cancelled_at = self.clock.now
        self.cancel_reason = reason

    async def terminate(self, reason: str) -> None:
        self.terminated_at = self.clock.now


async def watch(task: FakeTask, clock: FakeClock, **limits: float) -> run.Watched:
    return await run.watch(
        task,
        pause_timeout_s=limits.get("pause_timeout_s", 60.0),
        wall_limit_s=limits.get("wall_limit_s", 7200.0),
        poll_s=limits.get("poll_s", 2.0),
        cancel_grace_s=limits.get("cancel_grace_s", 30.0),
        clock=clock,
        sleep=clock.sleep,
    )


async def test_a_task_that_ends_by_itself_is_left_alone() -> None:
    clock = FakeClock()
    task = FakeTask(clock, [(0, Phase.CODING)], ends_at=20)
    watched = await watch(task, clock)
    assert watched.intervention_needed is False
    assert watched.stopped_by is None
    assert task.cancelled_at is None


async def test_a_pause_that_lasts_ends_the_task_after_one_minute() -> None:
    clock = FakeClock()
    task = FakeTask(clock, [(0, Phase.CODING), (30, Phase.PAUSED)])
    watched = await watch(task, clock)
    assert watched.intervention_needed is True
    assert watched.stopped_by is not None
    assert "paused" in watched.stopped_by
    assert task.cancel_reason == watched.stopped_by
    assert task.cancelled_at is not None
    # The pause is seen at the first poll after 30 s, and the timeout counts from there.
    assert 90 <= task.cancelled_at <= 94


async def test_a_pause_that_ends_in_time_still_counts_as_an_intervention() -> None:
    clock = FakeClock()
    task = FakeTask(clock, [(0, Phase.CODING), (10, Phase.PAUSED), (40, Phase.CODING)], ends_at=100)
    watched = await watch(task, clock)
    assert watched.intervention_needed is True
    assert watched.stopped_by is None
    assert task.cancelled_at is None


async def test_two_short_pauses_do_not_add_up_to_the_timeout() -> None:
    clock = FakeClock()
    phases: list[tuple[float, Phase | None]] = [
        (0, Phase.PAUSED),
        (40, Phase.CODING),
        (50, Phase.PAUSED),
        (90, Phase.CODING),
    ]
    task = FakeTask(clock, phases, ends_at=200)
    watched = await watch(task, clock)
    assert watched.intervention_needed is True
    assert task.cancelled_at is None


async def test_a_task_past_its_wall_limit_is_cancelled() -> None:
    clock = FakeClock()
    task = FakeTask(clock, [(0, Phase.CODING)])
    watched = await watch(task, clock, wall_limit_s=600)
    assert watched.intervention_needed is False
    assert watched.stopped_by is not None
    assert "time limit" in watched.stopped_by
    assert task.cancelled_at is not None
    assert 600 <= task.cancelled_at <= 604


async def test_a_task_that_ignores_the_cancel_is_terminated_after_the_grace_period() -> None:
    clock = FakeClock()
    task = FakeTask(clock, [(0, Phase.PAUSED)], stops_after=None)
    await watch(task, clock, cancel_grace_s=30)
    assert task.cancelled_at is not None
    assert task.terminated_at is not None
    assert 30 <= task.terminated_at - task.cancelled_at <= 34


async def test_a_status_that_no_worker_answered_is_not_a_pause() -> None:
    clock = FakeClock()
    task = FakeTask(clock, [(0, None)], ends_at=200)
    watched = await watch(task, clock)
    assert watched.intervention_needed is False
    assert task.cancelled_at is None


async def test_the_last_status_that_was_seen_is_kept() -> None:
    clock = FakeClock()
    task = FakeTask(clock, [(0, Phase.CODING)], ends_at=20)
    watched = await watch(task, clock)
    assert watched.status is not None
    assert watched.status.turn_index == 3


# ---------- reading the end of a task ----------
class FakeStore:
    def __init__(self, patch: str = "", error: Exception | None = None) -> None:
        self.patch = patch
        self.error = error
        self.asked: list[tuple[str, str]] = []

    async def diff(self, task_id: str, head_sha: str) -> str:
        self.asked.append((task_id, head_sha))
        if self.error is not None:
            raise self.error
        return self.patch


def status_at(checkpoint: str | None) -> TaskStatus:
    return TaskStatus(
        task_id="lh-eval-try1-calc-01",
        phase=Phase.DONE,
        turn_index=5,
        cost_usd=0.0,
        last_checkpoint=checkpoint,
        plan_version=None,
    )


async def test_the_patch_is_read_at_the_last_checkpoint_of_the_workflow() -> None:
    store = FakeStore("diff --git a/x b/x\n")
    patch, why = await run.read_patch(store, "lh-eval-try1-calc-01", status_at("a" * 40))  # type: ignore[arg-type]
    assert (patch, why) == ("diff --git a/x b/x\n", None)
    assert store.asked == [("lh-eval-try1-calc-01", "a" * 40)]


@pytest.mark.parametrize("final", [None, status_at(None)])
async def test_a_workflow_that_did_not_say_where_it_ended_has_no_patch(
    final: TaskStatus | None,
) -> None:
    patch, why = await run.read_patch(FakeStore(), "lh-eval-try1-calc-01", final)  # type: ignore[arg-type]
    assert patch == ""
    assert why is not None
    assert "where it ended" in why


async def test_a_checkpoint_repo_that_cannot_be_read_gives_an_empty_patch_and_a_reason() -> None:
    store = FakeStore(error=CheckpointError("the repo is gone"))
    patch, why = await run.read_patch(store, "lh-eval-try1-calc-01", status_at("b" * 40))  # type: ignore[arg-type]
    assert patch == ""
    assert why is not None
    assert "the repo is gone" in why


class FakeGrader:
    def __init__(self, result: GradeResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.graded: list[tuple[str, str, str | None]] = []

    async def grade(
        self, task: custom_tasks.CustomTask, patch: str, *, name: str | None = None
    ) -> GradeResult:
        self.graded.append((task.id, patch, name))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


def grading_context(grader: FakeGrader) -> run.RunContext:
    return SimpleNamespace(grader=grader)  # type: ignore[return-value]


def outcome_of(kind: OutcomeKind) -> Outcome:
    return Outcome(kind=kind, summary="done")


async def test_a_finished_task_is_graded_under_a_name_the_run_can_clean_up() -> None:
    grader = FakeGrader(GradeResult(True, "all tests passed (3 hidden, 4 repo)"))
    resolved, line = await run.grade(
        grading_context(grader),
        first_task(),
        outcome_of(OutcomeKind.PR_OPENED),
        "the patch",
        "lh-eval-try1-calc-01",
    )
    assert (resolved, line) == (True, "all tests passed (3 hidden, 4 repo)")
    assert grader.graded == [("calc-01", "the patch", "lh-eval-try1-calc-01-grade")]


async def test_a_patch_that_fails_the_grader_is_not_resolved() -> None:
    grader = FakeGrader(GradeResult(False, "3 hidden tests failed"))
    resolved, line = await run.grade(
        grading_context(grader),
        first_task(),
        outcome_of(OutcomeKind.PR_OPENED),
        "the patch",
        "lh-eval-try1-calc-01",
    )
    assert (resolved, line) == (False, "3 hidden tests failed")


@pytest.mark.parametrize(
    "kind",
    [OutcomeKind.STUCK, OutcomeKind.BUDGET_EXHAUSTED, OutcomeKind.CANCELLED, OutcomeKind.ERROR],
)
async def test_a_task_that_did_not_finish_is_not_graded_and_not_resolved(
    kind: OutcomeKind,
) -> None:
    grader = FakeGrader(GradeResult(True, "would pass"))
    resolved, line = await run.grade(
        grading_context(grader), first_task(), outcome_of(kind), "a patch", "lh-eval-try1-calc-01"
    )
    assert resolved is False
    assert kind.value in line
    assert grader.graded == []


async def test_a_grader_that_fails_leaves_the_task_ungraded() -> None:
    grader = FakeGrader(error=RuntimeError("docker is down"))
    resolved, line = await run.grade(
        grading_context(grader),
        first_task(),
        outcome_of(OutcomeKind.PR_OPENED),
        "the patch",
        "lh-eval-try1-calc-01",
    )
    assert resolved is None
    assert "docker is down" in line


def test_the_progress_line_names_the_task_the_outcome_and_the_verdict() -> None:
    result = TaskResult(
        run_id="try1",
        task_id="calc-01",
        workflow_id="lh-eval-try1-calc-01",
        task_set="custom",
        config="full",
        model="m",
        git_sha="f" * 40,
        provider=None,
        scripted=False,
        outcome="pr_opened",
        outcome_summary="done",
        resolved=None,
        grading="the grader failed: boom",
        cost_usd=0.5,
        tokens=Tokens(),
        cache_ratio=0.0,
        started_at=datetime(2026, 11, 4, tzinfo=UTC),
        ended_at=datetime(2026, 11, 4, 0, 1, tzinfo=UTC),
        wall_time_s=60.0,
        turns=5,
    )
    assert run.describe(result) == (
        "calc-01: pr_opened, not graded, $0.5000, 5 turns, 60 s (the grader failed: boom)"
    )


# ---------- a task that the runner could not finish ----------
class Remover:
    def __init__(self) -> None:
        self.removed: list[str] = []

    async def cleanup_task(self, task_id: str) -> None:
        self.removed.append(task_id)


def run_context(tmp_path: Path, *extra: str) -> run.RunContext:
    options = run.parse_args([*BASE_ARGS, "--results-dir", str(tmp_path), *extra])
    return SimpleNamespace(  # type: ignore[return-value]
        options=options,
        settings=SimpleNamespace(task_queue="q"),
        client=None,
        blobs=None,
        store=Remover(),
        sandboxes=Remover(),
        sessions=None,
        model="claude-sonnet-5-5",
        commit="f" * 40,
        folder=tmp_path,
        driver=None,
    )


def fail_to_start(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    async def start_task(*_args: object, **_kwargs: object) -> None:
        raise error

    async def upload_script(*_args: object, **_kwargs: object) -> str:
        return "the-script-ref"

    monkeypatch.setattr(run, "start_task", start_task)
    monkeypatch.setattr(run, "upload_script", upload_script)


def ledger_says(monkeypatch: pytest.MonkeyPatch, usd: float, tokens: Tokens) -> None:
    async def ledger_totals(*_args: object) -> tuple[float, Tokens]:
        return usd, tokens

    monkeypatch.setattr(run, "ledger_totals", ledger_totals)


async def test_a_task_that_the_runner_cannot_run_becomes_an_error_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fail_to_start(monkeypatch, RuntimeError("the database is down"))
    ledger_says(monkeypatch, 0.0, Tokens())
    ctx = run_context(tmp_path)

    result = await run.run_one(ctx, first_task())

    assert (result.outcome, result.resolved) == ("error", None)
    assert "the database is down" in result.outcome_summary
    assert "the database is down" in result.grading
    assert (result.run_id, result.task_id) == ("try1", "calc-01")
    assert result.workflow_id == "lh-eval-try1-calc-01"
    assert (result.model, result.git_sha) == ("claude-sonnet-5-5", "f" * 40)
    assert result.ended_at >= result.started_at
    # The result is on disk like any other, and what the task left behind is removed.
    assert results.read_results(tmp_path) == [result]
    assert ctx.sandboxes.removed == ["lh-eval-try1-calc-01"]  # type: ignore[attr-defined]
    assert ctx.store.removed == ["lh-eval-try1-calc-01"]  # type: ignore[attr-defined]


async def test_an_error_result_keeps_the_cost_that_was_spent_before_the_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fail_to_start(monkeypatch, RuntimeError("boom"))
    ledger_says(monkeypatch, 0.75, Tokens(input=100, output=20, cache_read=300))

    result = await run.run_one(run_context(tmp_path), first_task())

    assert result.cost_usd == 0.75
    assert result.tokens == Tokens(input=100, output=20, cache_read=300)
    assert result.cache_ratio == 0.75


async def test_an_error_result_is_written_even_when_the_ledger_is_the_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fail_to_start(monkeypatch, RuntimeError("boom"))

    async def broken_ledger(*_args: object) -> tuple[float, Tokens]:
        raise RuntimeError("the ledger is unreachable")

    monkeypatch.setattr(run, "ledger_totals", broken_ledger)

    result = await run.run_one(run_context(tmp_path), first_task())

    assert (result.outcome, result.cost_usd, result.tokens) == ("error", 0.0, Tokens())
    assert "boom" in result.outcome_summary


async def test_an_error_result_says_where_the_answers_came_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fail_to_start(monkeypatch, RuntimeError("boom"))
    ledger_says(monkeypatch, 0.0, Tokens())
    script = tmp_path / "calc-01.yaml"
    script.write_text("steps: []\n", encoding="utf-8")

    played = await run.run_one(run_context(tmp_path / "a", "--script", str(script)), first_task())
    real = await run.run_one(run_context(tmp_path / "b"), first_task())

    assert (played.provider, played.scripted) == ("scripted", True)
    assert (real.provider, real.scripted) == (None, False)


async def test_a_stop_of_the_whole_run_is_not_turned_into_a_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Ctrl-C ends the run. That is not a failure of one task, and no result is written for it.
    fail_to_start(monkeypatch, asyncio.CancelledError())
    ledger_says(monkeypatch, 0.0, Tokens())
    with pytest.raises(asyncio.CancelledError):
        await run.run_one(run_context(tmp_path), first_task())
    assert list(tmp_path.glob("*.json")) == []


def result_for(task: custom_tasks.CustomTask) -> TaskResult:
    now = datetime(2026, 11, 4, tzinfo=UTC)
    return TaskResult(
        run_id="try1",
        task_id=task.id,
        workflow_id=f"lh-eval-try1-{task.id}",
        task_set="custom",
        config="full",
        model="claude-sonnet-5-5",
        git_sha="f" * 40,
        provider=None,
        scripted=False,
        outcome="pr_opened",
        outcome_summary="done",
        resolved=True,
        grading="all tests passed",
        cost_usd=0.1,
        tokens=Tokens(),
        cache_ratio=0.0,
        started_at=now,
        ended_at=now,
        wall_time_s=1.0,
        turns=5,
    )


async def test_one_task_that_fails_does_not_stop_the_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tasks = custom_tasks.load_tasks()[:3]
    finished: list[str] = []

    async def execute_task(
        _ctx: object, task: custom_tasks.CustomTask, _started_at: datetime
    ) -> TaskResult:
        if task is tasks[1]:
            raise RuntimeError("the status query failed")
        await asyncio.sleep(0.05)  # still running when the second task fails
        finished.append(task.id)
        return result_for(task)

    monkeypatch.setattr(run, "execute_task", execute_task)
    ledger_says(monkeypatch, 0.0, Tokens())
    ctx = run_context(tmp_path, "--concurrency", "3")

    found = await run.run_tasks(ctx, tasks)

    assert [r.task_id for r in found] == [t.id for t in tasks]
    assert finished == [tasks[0].id, tasks[2].id]
    assert [r.outcome for r in found] == ["pr_opened", "error", "pr_opened"]
    # The summary can be written for what ran, with the failed task counted and not graded.
    summary = results.summarize(found, started_at=found[0].started_at, ended_at=found[0].ended_at)
    assert (summary.attempted, summary.resolved, summary.ungraded) == (3, 2, 1)


def main_with_a_stub(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *flags: str
) -> str:
    """Run main() with a stub in place of the run, and return what it wrote to stderr."""
    task = first_task()
    done = result_for(task)

    async def run_eval(_options: RunOptions, _settings: object) -> results.Summary:
        return results.summarize([done], started_at=done.started_at, ended_at=done.ended_at)

    monkeypatch.setattr(run, "run_eval", run_eval)
    assert run.main([*BASE_ARGS, *flags]) == 0
    return capsys.readouterr().err


def test_a_provider_the_runner_cannot_apply_is_noted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    err = main_with_a_stub(monkeypatch, capsys, "--provider", "anthropic")
    assert "--provider" in err
    assert "worker" in err


def test_a_run_with_the_defaults_has_nothing_to_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main_with_a_stub(monkeypatch, capsys) == ""


# ---------- the SWE-bench set (LH-902) ----------
def test_the_swebench_tasks_are_the_first_instances_of_the_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval.swebench import adapter

    asked: list[int | None] = []
    found = [object(), object()]

    def select_instances(limit: int | None) -> list[object]:
        asked.append(limit)
        return found

    monkeypatch.setattr(adapter, "select_instances", select_instances)
    options = run.parse_args(
        ["--set", "swebench", "--config", "full", "--run-id", "try1", "--limit", "2"]
    )

    assert run.select_tasks(options) == found
    assert asked == [2]


def test_the_custom_set_still_comes_from_tasks_yaml() -> None:
    options = run.parse_args(
        ["--set", "custom", "--config", "full", "--run-id", "try1", "--limit", "2"]
    )

    assert [t.id for t in run.select_tasks(options)] == [
        t.id for t in custom_tasks.load_tasks()[:2]
    ]


def test_the_swebench_set_has_its_own_dollar_cap_and_the_same_turn_and_time_caps() -> None:
    # Plan section 8.4: task_usd 4 on SWE-bench and 2 on the custom set.
    assert run.SWEBENCH_BUDGETS.task_usd == 4.0
    assert run.CUSTOM_BUDGETS.task_usd == 2.0
    assert run.SWEBENCH_BUDGETS.task_turns == run.CUSTOM_BUDGETS.task_turns == 150
    assert run.SWEBENCH_BUDGETS.task_wall_minutes == run.CUSTOM_BUDGETS.task_wall_minutes == 120
