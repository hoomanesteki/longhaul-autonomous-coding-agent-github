"""The results report on dry-run result files (LH-905).

tests/fixtures/eval_results holds runs in the shape that eval/run.py writes, every one of them
scripted (scripts/make_eval_result_fixtures.py makes them). They cover each section of the page:
three configs on the SWE-bench set and on the custom set, a second run of `full`, the compaction
and memory ablations, and the summary of a chaos suite. The page is built from them into a
temporary folder, and the numbers on it are checked against a count made here from the files.

This needs no stack. It is an integration test because it runs the whole builder, the charts
included, and writes real files.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from eval.report import build_report

pytestmark = [pytest.mark.integration, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "eval_results"
GIT_SHA = "abc1234def5678abc1234def5678abc1234def56"

# The order matters: the first run that is not an ablation names the headline model.
RUN_IDS = [
    "swe-single_shot",
    "swe-naive",
    "swe-full",
    "custom-single_shot",
    "custom-naive",
    "custom-full",
    "custom-full-2",
    "long-compact",
    "long-no-compaction",
    "memory-on",
    "memory-off",
    "chaos-dryrun",
]

HEADINGS = [
    "# Results",
    "## Method",
    "## Table 1: SWE-bench Verified subset",
    "## Table 2: Custom set",
    "## Tokens per task",
    "## Table 3: Ablations",
    "## Table 4: Chaos",
    "## Table 5: Unresolved tasks",
    "## Runs used",
]

HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None


def solved(run_id: str) -> dict[str, bool]:
    """task id -> resolved, counted straight from the result files of a run."""
    found: dict[str, bool] = {}
    for path in sorted((FIXTURES / run_id).glob("*.json")):
        if path.name != "summary.json":
            data = json.loads(path.read_text(encoding="utf-8"))
            found[data["task_id"]] = data["resolved"] is True
    return found


@pytest.fixture(scope="module")
def page(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    out_dir = tmp_path_factory.mktemp("docs")
    out = out_dir / "results.md"
    text = build_report.build(
        RUN_IDS,
        results_dir=FIXTURES,
        out=out,
        img_dir=out_dir / "img",
        git_sha=GIT_SHA,
        allow_scripted=True,
    )
    assert out.read_text(encoding="utf-8") == text
    return text, out_dir


def test_sections_present(page: tuple[str, Path]) -> None:
    text, _ = page
    # A heading may carry the size of its set, as in "Table 1: ... (n=40)".
    found = [
        next(wanted for wanted in HEADINGS if line.startswith(wanted))
        for line in text.splitlines()
        if any(line.startswith(wanted) for wanted in HEADINGS)
    ]
    assert found == HEADINGS
    # A section that has data does not say that it has none.
    for title in ("Table 1", "Table 2", "Table 3", "Table 4", "Table 5"):
        body = text.split(f"## {title}")[1].split("\n## ")[0]
        assert "No run" not in body, title
        assert "|" in body, title


def test_results_table_has_a_row_per_config_with_intervals(page: tuple[str, Path]) -> None:
    text, _ = page
    table = text.split("## Table 1")[1].split("\n## ")[0]
    for config in ("single_shot", "naive", "full"):
        row = next(line for line in table.splitlines() if line.startswith(f"| {config} "))
        done = sum(solved(f"swe-{config}").values())
        assert f"| {done} / 40 |" in row
        assert f"{100 * done / 40:.1f}%" in row
        assert " to " in row  # the Wilson interval


def test_mcnemar_pairs_come_from_the_files(page: tuple[str, Path]) -> None:
    text, _ = page
    full, naive = solved("swe-full"), solved("swe-naive")
    b = sum(1 for t in full if full[t] and not naive[t])
    c = sum(1 for t in full if naive[t] and not full[t])
    line = next(line for line in text.splitlines() if line.startswith("McNemar") and "b = " in line)
    assert f"b = {b}, c = {c}" in line
    assert re.search(r"p (=|<) \d\.\d+", line)


def test_footer_names_the_runs_and_the_commit(page: tuple[str, Path]) -> None:
    text, _ = page
    footer = text.split("## Runs used")[1]
    for run_id in RUN_IDS:
        assert run_id in footer
    assert GIT_SHA in footer
    # The runs are scripted, and the page says so before any number.
    assert "dry run" in text.split("## Method")[0].lower()


def test_chaos_table_is_filled_from_the_summary(page: tuple[str, Path]) -> None:
    text, _ = page
    summary = json.loads((FIXTURES / "chaos-dryrun" / "summary.json").read_text(encoding="utf-8"))
    table = text.split("## Table 4")[1].split("\n## ")[0]
    for label in ("Worker SIGKILL", "Sandbox kill", "Temporal restart", "Postgres restart", "All"):
        assert label in table
    assert f"{summary['completed']} / {summary['runs']}" in table
    assert "Retry cost overhead" in table


def test_unresolved_tasks_are_listed_with_a_reason(page: tuple[str, Path]) -> None:
    text, _ = page
    table = text.split("## Table 5")[1].split("\n## ")[0]
    rows = [line for line in table.splitlines() if line.startswith("| fixture__swe-")]
    unresolved = [t for t, ok in solved("swe-full").items() if not ok]
    assert len(rows) >= len(unresolved)
    for task in unresolved:
        assert any(f"| {task} |" in row for row in rows), task
    header = next(line for line in table.splitlines() if line.startswith("| Task |"))
    assert all(row.count("|") == header.count("|") for row in rows)


def test_charts_are_written_next_to_the_page(page: tuple[str, Path]) -> None:
    text, out_dir = page
    names = ("tokens-per-task.png", "resolve-rates.png")
    if HAS_MATPLOTLIB:
        for name in names:
            image = out_dir / "img" / name
            assert image.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            assert f"](img/{name})" in text
    else:
        # CI installs no extras. The page says what is missing and writes no image.
        assert "uv sync --extra eval" in text
        assert not any((out_dir / "img" / name).exists() for name in names)


def test_the_same_runs_give_the_same_page(page: tuple[str, Path], tmp_path: Path) -> None:
    text, _ = page
    again = build_report.build(
        RUN_IDS,
        results_dir=FIXTURES,
        out=tmp_path / "results.md",
        img_dir=tmp_path / "img",
        git_sha=GIT_SHA,
        allow_scripted=True,
    )
    assert again == text


def test_command_line_writes_the_placeholder_without_runs(tmp_path: Path) -> None:
    out = tmp_path / "results.md"
    done = subprocess.run(
        [sys.executable, str(ROOT / "eval" / "report" / "build_report.py"), "--out", str(out)],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert out.read_text(encoding="utf-8") == build_report.placeholder_page()
