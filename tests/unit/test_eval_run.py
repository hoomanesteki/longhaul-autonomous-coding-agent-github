"""The options, the task spec and the watch loop of eval/run.py (LH-901).

No Temporal, Docker or database here. The watch loop gets a fake task and a fake clock, so the
one-minute pause timeout costs no time. The runner against the real stack is in
tests/integration/test_eval_runner.py.
"""

from pathlib import Path

import pytest

from eval import custom_tasks, run
from eval.run import RunOptions
from longhaul.checkpoints.git_store import check_task_id
from longhaul.contracts import Phase, TaskStatus
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


def test_the_swebench_set_is_not_there_yet(capsys: pytest.CaptureFixture[str]) -> None:
    err = refuses(capsys, ["--set", "swebench", "--config", "full", "--run-id", "try1"])
    assert "swebench" in err
    assert "LH-902" in err


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


def test_an_empty_run_folder_can_be_used_again(tmp_path: Path) -> None:
    run.claim_run_dir(tmp_path, "try1")
    assert run.claim_run_dir(tmp_path, "try1") == tmp_path / "try1"


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
