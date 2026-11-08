"""The parts of the results report that decide what a number means (LH-905).

Which runs make the headline tables, which run an ablation is paired with, what the one-line
reason of an unresolved task is, how numbers are written, and when the builder refuses to write a
page. The page built from whole folders of results is tested in
tests/integration/test_report_build.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eval import results
from eval.chaos import results as chaos_results
from eval.chaos.results import Summary as ChaosSummary
from eval.report import build_report, runs, sections
from eval.report.runs import ChaosRun
from eval.results import TaskResult, Tokens

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
START = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def result(run_id: str, task_id: str, **changes: Any) -> TaskResult:
    values: dict[str, Any] = {
        "run_id": run_id,
        "task_id": task_id,
        "workflow_id": f"lh-eval-{run_id}-{task_id}",
        "task_set": "custom",
        "config": "full",
        "config_hash": "a" * 64,
        "seed": 42,
        "model": "sonnet-x",
        "git_sha": "f" * 40,
        "provider": None,
        "scripted": False,
        "outcome": "pr_opened",
        "outcome_summary": "Changed add().",
        "resolved": True,
        "grading": "all tests passed",
        "cost_usd": 0.5,
        "tokens": Tokens(input=800, output=100, cache_read=200),
        "cache_ratio": 0.2,
        "started_at": START,
        "ended_at": START + timedelta(seconds=60),
        "wall_time_s": 60.0,
        "turns": 5,
    }
    return TaskResult(**{**values, **changes})


def make_run(run_id: str, tasks: list[str], **changes: Any) -> runs.EvalRun:
    return runs.EvalRun(run_id, tuple(result(run_id, t, **changes) for t in tasks))


def write_run(folder: Path, run: runs.EvalRun) -> None:
    for item in run.results:
        results.write_result(folder / run.run_id, item)


# ---------- how numbers are written ----------
def test_numbers_are_written_the_same_way_everywhere() -> None:
    assert sections.percent(0.725) == "72.5%"
    assert sections.interval((0.52018, 0.79915)) == "52.0% to 79.9%"
    assert sections.money(1.5) == "$1.50"
    assert sections.money(0.0473) == "$0.047"
    assert sections.money(None) == "n/a"
    assert sections.minutes(750.0) == "12.5 min"
    assert sections.minutes(None) == "n/a"
    assert sections.p_value(0.0006) == "<0.001"
    assert sections.p_value(0.109375) == "0.109"
    assert sections.p_text(0.0006) == "p < 0.001"
    assert sections.p_text(0.109375) == "p = 0.109"


# ---------- which runs are the headline ----------
def test_arrange_names_the_headline_model_from_the_first_plain_run() -> None:
    plain = make_run("h1", ["a", "b"], task_set="swebench", config="naive")
    other = make_run("h2", ["a", "b"], task_set="swebench", config="full", model="other")
    layout = runs.arrange([plain, other])
    assert layout.headline_model == "sonnet-x"
    assert list(layout.swebench) == ["naive"]  # the run on another model is not a headline run


def test_a_second_full_run_on_a_set_is_kept_in_order() -> None:
    first = make_run("c1", ["a"], config="full")
    second = make_run("c2", ["a"], config="full")
    layout = runs.arrange([first, second])
    assert [r.run_id for r in layout.custom["full"]] == ["c1", "c2"]


def test_ablation_runs_are_not_headline_runs() -> None:
    plain = make_run("c1", ["a", "b"])
    off = make_run("c2", ["a", "b"], memory="off")
    squeezed = make_run("c3", ["a", "b"], context_mode="no_compaction")
    layout = runs.arrange([plain, off, squeezed])
    assert [r.run_id for r in layout.custom["full"]] == ["c1"]


def test_an_ablation_is_paired_with_a_run_of_its_model_on_the_same_tasks() -> None:
    wrong_model = make_run("w", ["a", "b", "c"], model="elsewhere")
    with_side = make_run("with", ["a", "b", "c"], model="haiku-x")
    without = make_run("without", ["b", "c", "d"], model="haiku-x", context_mode="no_compaction")
    layout = runs.arrange([make_run("head", ["a"]), wrong_model, with_side, without])
    (pairing,) = layout.compaction
    assert (pairing.with_run.run_id, pairing.without_run.run_id) == ("with", "without")
    assert pairing.task_ids == ("b", "c")  # only the tasks that both runs attempted
    assert layout.memory == []


def test_the_partner_is_the_plain_run_that_covers_the_most_tasks() -> None:
    narrow = make_run("narrow", ["a", "b"], model="haiku-x")
    wide = make_run("wide", ["a", "b", "c", "d"], model="haiku-x")
    off = make_run("off", ["a", "b", "c"], model="haiku-x", memory="off")
    (pairing,) = runs.arrange([make_run("head", ["a"]), narrow, wide, off]).memory
    assert pairing.with_run.run_id == "wide"


def test_an_ablation_without_a_partner_is_left_out_and_does_not_fail() -> None:
    lonely = make_run("without", ["a"], model="haiku-x", memory="off")
    assert runs.arrange([make_run("head", ["a"]), lonely]).memory == []


# ---------- what is unresolved, and why ----------
def test_reason_of_a_stuck_task_is_the_stuck_report() -> None:
    stuck = result(
        "r",
        "t",
        outcome="stuck",
        resolved=False,
        stuck_reasons=["same test failed six times"],
        grading="no patch to grade",
    )
    assert sections.reason_for(stuck) == "stuck: same test failed six times"


def test_reason_of_a_graded_task_is_the_grader_line() -> None:
    failed = result("r", "t", resolved=False, grading="2 tests failed")
    assert sections.reason_for(failed) == "2 tests failed"


def test_reason_falls_back_to_the_outcome_summary() -> None:
    empty = result("r", "t", resolved=False, grading="", outcome_summary="Ran out of budget.")
    assert sections.reason_for(empty) == "Ran out of budget."


def test_reason_of_a_task_that_never_reached_the_grader_is_why_it_stopped() -> None:
    # The shape eval/run.py writes: the grader line only repeats the outcome, stuck_reasons stays
    # empty, and the stuck report or the error text is in outcome_summary.
    cases = {
        "stuck": "stuck, the same failing test came back after six edits; no hint in 24 h",
        "budget_exhausted": "the task spent its 2.00 dollar cap before it finished",
        "error": "the sandbox could not start: image not found",
    }
    for kind, why in cases.items():
        ended = result(
            "r",
            "t",
            outcome=kind,
            resolved=False,
            grading=f"not graded: the task ended as {kind}",
            outcome_summary=why,
            stuck_reasons=[],
        )
        assert sections.reason_for(ended) == why


def test_reason_of_an_ungraded_pull_request_is_the_runner_message() -> None:
    ungraded = result(
        "r",
        "t",
        resolved=None,
        grading="not graded: the grader failed to start",
        outcome_summary="Opened a pull request.",
    )
    assert sections.reason_for(ungraded) == "not graded: the grader failed to start"


def test_reason_is_one_line_that_cannot_break_a_table() -> None:
    messy = result("r", "t", resolved=False, grading="a | b\nsecond line " + "x" * 400)
    reason = sections.reason_for(messy)
    assert "\n" not in reason and "|" not in reason.replace("\\|", "")
    assert len(reason) <= 160


# ---------- what counts as resolved ----------
def test_an_ungraded_task_counts_as_attempted_and_not_resolved() -> None:
    run = runs.EvalRun(
        "r",
        (
            result("r", "a"),
            result("r", "b", resolved=False),
            result("r", "c", resolved=None, grading="not graded yet"),
        ),
    )
    assert (run.attempted, run.resolved, run.ungraded) == (3, 1, 1)
    assert run.solved("c") is False


def test_silent_failures_are_tasks_that_end_without_a_report() -> None:
    kinds = ["pr_opened", "stuck", "budget_exhausted", "cancelled", "error"]
    run = runs.EvalRun("r", tuple(result("r", k, outcome=k, resolved=False) for k in kinds))
    assert run.silent_failures == 2  # cancelled and error leave nothing on the issue


# ---------- loading ----------
def test_a_run_id_without_a_folder_is_named_in_the_error(tmp_path: Path) -> None:
    with pytest.raises(runs.ReportError, match="no-such-run"):
        runs.load_runs(tmp_path, ["no-such-run"])


def test_a_folder_of_one_run_is_loaded_whole(tmp_path: Path) -> None:
    write_run(tmp_path, make_run("r1", ["a", "b"]))
    loaded = runs.load_runs(tmp_path, ["r1"])
    assert [r.task_id for r in loaded.eval_runs[0].results] == ["a", "b"]
    assert loaded.chaos_runs == ()


def test_results_of_two_runs_in_one_folder_are_refused(tmp_path: Path) -> None:
    write_run(tmp_path, make_run("r1", ["a"]))
    results.write_result(tmp_path / "r1", result("r2", "b"))
    with pytest.raises(runs.ReportError, match="run_id"):
        runs.load_runs(tmp_path, ["r1"])


def test_a_chaos_summary_is_told_from_an_eval_run(tmp_path: Path) -> None:
    folder = tmp_path / "chaos-x"
    folder.mkdir()
    source = ROOT / "eval" / "results" / "chaos-2026-11-07" / "summary.json"
    (folder / "summary.json").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    loaded = runs.load_runs(tmp_path, ["chaos-x"])
    assert loaded.eval_runs == ()
    assert loaded.chaos_runs[0].summary.runs == 20
    assert loaded.chaos_runs[0].scripted  # the recordings of that suite were scripted


# ---------- when the builder writes a page ----------
def test_a_scripted_run_needs_the_dry_run_flag(tmp_path: Path) -> None:
    write_run(tmp_path, make_run("dry", ["a"], provider="scripted", scripted=True))
    with pytest.raises(runs.ReportError, match="--allow-scripted"):
        build_report.build(
            ["dry"],
            results_dir=tmp_path,
            out=tmp_path / "out" / "results.md",
            img_dir=tmp_path / "out" / "img",
            git_sha="f" * 40,
        )
    assert not (tmp_path / "out").exists()  # a refused build writes nothing


def test_no_runs_writes_the_placeholder_that_is_committed(tmp_path: Path) -> None:
    out = tmp_path / "results.md"
    text = build_report.build(
        [], results_dir=tmp_path, out=out, img_dir=tmp_path / "img", git_sha="f" * 40
    )
    assert text == build_report.placeholder_page()
    assert out.read_text(encoding="utf-8") == text
    assert not (tmp_path / "img").exists()


def test_the_committed_page_is_the_placeholder_or_a_page_the_builder_wrote() -> None:
    # docs/results.md is generated. Until a real run exists it is exactly the page that the builder
    # writes for no runs. After a real run it is that run's page, which names its runs and its
    # commit, so the check rebuilds it from the result files and compares.
    committed = (ROOT / "docs" / "results.md").read_text(encoding="utf-8")
    if committed == build_report.placeholder_page():
        return
    rebuilt = build_report.rebuild(
        committed,
        results_dir=ROOT / "eval" / "results",
        out=ROOT / "docs" / "results.md",
        img_dir=ROOT / "docs" / "img",
    )
    assert committed == rebuilt


def test_a_built_page_is_rebuilt_to_the_same_text(tmp_path: Path) -> None:
    write_run(tmp_path, make_run("r1", ["a", "b"]))
    out = tmp_path / "out" / "results.md"
    img = tmp_path / "out" / "img"
    text = build_report.build(["r1"], results_dir=tmp_path, out=out, img_dir=img, git_sha="c" * 40)
    assert build_report.rebuild(text, results_dir=tmp_path, out=out, img_dir=img) == text
    edited = text.replace("# Results", "# Results of my own", 1)
    assert build_report.rebuild(edited, results_dir=tmp_path, out=out, img_dir=img) != edited


def test_the_provenance_of_a_built_page_is_its_run_ids_and_its_commit(tmp_path: Path) -> None:
    write_run(tmp_path, make_run("r1", ["a", "b"]))
    write_run(tmp_path, make_run("r2", ["a", "b"], config="naive"))
    text = build_report.build(
        ["r1", "r2"],
        results_dir=tmp_path,
        out=tmp_path / "out" / "results.md",
        img_dir=tmp_path / "out" / "img",
        git_sha="c" * 40,
    )
    assert build_report.provenance(text) == (["r1", "r2"], "c" * 40)


def test_the_placeholder_gives_the_commands_to_make_the_real_page() -> None:
    page = build_report.placeholder_page()
    assert "No real run has been made yet" in page
    for command in ("eval/run.py", "eval/chaos/chaos.py", "eval/report/build_report.py"):
        assert command in page


def test_the_placeholder_records_the_chaos_tasks_with_a_model_first() -> None:
    # Without the recording step the suite replays scripts, and the builder refuses its page.
    page = build_report.placeholder_page()
    record = page.index("eval/chaos/chaos.py --record --provider anthropic")
    suite = page.index("eval/chaos/chaos.py --runs")
    assert record < suite
    assert "ANTHROPIC_API_KEY" in page


def test_the_placeholder_runs_the_memory_ablation_on_a_worker_with_memory_off() -> None:
    # --memory off is only recorded, so the worker has to be started with memory off.
    page = build_report.placeholder_page()
    warm = page.index("--run-id abl-warmup")
    with_memory = page.index("--run-id abl-full")
    restart = page.index("memory.enabled: false")
    without = page.index("--run-id abl-no-memory")
    assert warm < with_memory < restart < without
    assert "LONGHAUL_CONFIG" in page


# ---------- table 4 ----------
def chaos_summary(tmp_path: Path) -> ChaosSummary:
    """The committed suite with one run that did not complete and so has no diff to compare."""
    source = ROOT / "eval" / "results" / "chaos-2026-11-07" / "summary.json"
    base = ChaosSummary.model_validate_json(source.read_text(encoding="utf-8"))
    found = [r.model_copy() for r in base.results]
    broken = next(r for r in found if r.kills and r.kills[0].landed)
    broken.completed = False
    broken.diff_equals_reference = False
    broken.outcome = "error"
    return chaos_results.summarize(
        found,
        name=base.name,
        seed=base.seed,
        git_sha=base.git_sha,
        tasks=base.tasks,
        recordings=base.recordings,
        started_at=base.started_at,
        ended_at=base.ended_at,
        clock_offset_s=base.clock_offset_s,
        compose_project=base.compose_project,
    )


def test_diff_equality_is_counted_over_completed_runs_in_every_row(tmp_path: Path) -> None:
    summary = chaos_summary(tmp_path)
    assert summary.completed < summary.runs
    rows = [
        [c.strip() for c in line.strip("|").split("|")]
        for line in sections.chaos_table(summary).splitlines()[2:]
    ]
    for row in rows:
        completed = row[3].split(" / ")
        diff = row[4].split(" / ")
        assert diff[1] == completed[0], row  # the denominator is the runs that completed
        assert int(diff[0]) <= int(diff[1]), row
    assert rows[-1][4].endswith(f"/ {summary.completed}")


def test_the_chaos_section_puts_a_blank_line_after_its_heading() -> None:
    summary = ChaosSummary.model_validate_json(
        (ROOT / "eval" / "results" / "chaos-2026-11-07" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    text = sections.chaos_section([ChaosRun("chaos-x", summary)])
    assert text.startswith("## Table 4: Chaos\n\nSuite ")


def test_one_repeated_ledger_row_is_written_in_the_singular() -> None:
    assert sections.count_of(1, "repeated ledger row") == "1 repeated ledger row"
    assert sections.count_of(3, "repeated ledger row") == "3 repeated ledger rows"
    assert sections.count_of(0, "repeated ledger row") == "0 repeated ledger rows"


def test_a_median_of_turns_keeps_its_half() -> None:
    assert sections.half(16.5) == "16.5"
    assert sections.half(16.0) == "16"
