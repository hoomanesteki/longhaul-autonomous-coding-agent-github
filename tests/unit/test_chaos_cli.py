"""The command line of eval/chaos/chaos.py (LH-904).

`uv run python eval/chaos/chaos.py --runs 20 --seed 7` is the full suite. These tests cover the
flags and the folder the results go to. Running the suite needs Docker, Temporal and Postgres,
and tests/chaos/test_chaos_full.py does that.
"""

from datetime import date
from pathlib import Path

import pytest

from eval.chaos import chaos
from eval.chaos.plan import DEFAULT_RUNS, DEFAULT_SEED, RECORDED_TASKS

pytestmark = [pytest.mark.unit, pytest.mark.phase9]


def test_without_flags_it_is_the_suite_of_the_story() -> None:
    options = chaos.parse_args([])

    assert (options.runs, options.seed) == (DEFAULT_RUNS, DEFAULT_SEED) == (20, 7)
    assert options.tasks == RECORDED_TASKS
    assert options.results_dir == chaos.DEFAULT_RESULTS_DIR
    assert options.record is False


def test_runs_and_seed_come_from_the_flags() -> None:
    options = chaos.parse_args(["--runs", "2", "--seed", "11"])

    assert (options.runs, options.seed) == (2, 11)


@pytest.mark.parametrize("flag", ["--runs", "--seed"])
@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_a_count_or_seed_that_is_not_above_zero_is_a_usage_error(flag: str, value: str) -> None:
    with pytest.raises(SystemExit) as stopped:
        chaos.parse_args([flag, value])

    assert stopped.value.code == 2


def test_tasks_are_chosen_by_id_from_the_recorded_ones() -> None:
    options = chaos.parse_args(["--tasks", "calc-01,textkit-02"])

    assert options.tasks == ("calc-01", "textkit-02")


def test_a_task_that_has_no_recording_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        chaos.parse_args(["--tasks", "calc-01,calc-09"])

    assert stopped.value.code == 2
    assert "calc-09" in capsys.readouterr().err


def test_the_results_folder_is_named_for_the_day() -> None:
    assert chaos.folder_name(date(2026, 11, 5)) == "chaos-2026-11-05"


def test_the_name_of_the_folder_can_be_set_and_cannot_leave_the_results_folder() -> None:
    assert chaos.parse_args(["--name", "chaos-try-1"]).name == "chaos-try-1"
    with pytest.raises(SystemExit) as stopped:
        chaos.parse_args(["--name", "../elsewhere"])

    assert stopped.value.code == 2


def test_a_results_folder_is_never_used_a_second_time(tmp_path: Path) -> None:
    first = chaos.claim_folder(tmp_path, "chaos-2026-11-05")

    assert first == tmp_path / "chaos-2026-11-05" and first.is_dir()
    with pytest.raises(ValueError, match="exists"):
        chaos.claim_folder(tmp_path, "chaos-2026-11-05")


def test_record_mode_takes_a_provider_and_the_suite_does_not() -> None:
    assert chaos.parse_args(["--record"]).provider == "scripted"
    assert chaos.parse_args(["--record", "--provider", "anthropic"]).provider == "anthropic"
    with pytest.raises(SystemExit) as stopped:
        chaos.parse_args(["--provider", "anthropic"])

    assert stopped.value.code == 2


def test_a_real_model_cannot_be_asked_for_without_its_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert chaos.main(["--record", "--provider", "anthropic"]) == 2

    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
