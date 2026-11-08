"""Build docs/results.md and its charts from result files (LH-905).

    uv run python eval/report/build_report.py RUN_ID [RUN_ID ...]

Each RUN_ID names a folder under eval/results: a run of eval/run.py, or the suite of
eval/chaos/chaos.py. List the headline runs first, because the first run that is not an ablation
names the headline model (eval/report/runs.py). The page holds the tables of plan section 8.6, the
Wilson intervals, the McNemar test of full against naive, dollars per resolved, a box plot of
tokens per task, the paired ablations, the chaos table, and every unresolved task with a reason.
The footer lists the run ids and the git sha, and the build adds no date, so the same files give
the same page.

Two rules keep the page honest.

* With no run ids the builder writes `placeholder_page()`: a page that says that no real run
  exists and how to make one. docs/results.md is that page until a real run is reported, and a
  unit test keeps the committed file equal to it. Numbers from fixtures never go into it.
* A scripted run (`provider: scripted`) says nothing about a model. The builder refuses it unless
  `--allow-scripted` is given, and then it puts a dry-run notice above the first number. That flag
  is how the tests and a local try-out build a full page from fixtures.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

if __package__ in (None, ""):  # started as `python eval/report/build_report.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from eval.report import charts, runs, sections  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RESULTS_DIR = ROOT / "eval" / "results"
DEFAULT_OUT = ROOT / "docs" / "results.md"
DEFAULT_IMG_DIR = ROOT / "docs" / "img"

PLACEHOLDER = """\
# Results

No real run has been made yet, so this page has no numbers. It does not guess.

`eval/report/build_report.py` writes this page from the result files of eval runs, and every
number on the finished page comes from those files. The report holds:

* the results table of each config on the SWE-bench Verified subset and on the custom set, with a
  Wilson 95% interval on each resolve rate, dollars per resolved task, median wall time, tokens
  per task and the cache ratio
* a McNemar test of full against naive, paired on the same tasks
* a box plot of tokens per task
* the paired ablations: compaction on and off, and memory on and off
* the chaos table: completion, diff equality, duplicate side effects and resume latency by kill type
* every unresolved task of the full config, with its outcome and a one-line reason
* a footer with the run ids and the git sha

The method, the task sets and the formulas are in section 8 of the plan.

## Produce the real page

Start the stack and the worker as in the runbook ("Run the evaluation"), then run each config on
each set with one model, and grade the SWE-bench runs. The first command of each group names the
run set, so the runner refuses a second model inside it.

```bash
make up && make sandbox-image && make migrate
uv sync --extra eval

RUN="uv run python eval/run.py"
$RUN --set swebench --config single_shot --run-id swe-single-shot --run-set swe-headline
$RUN --set swebench --config naive --run-id swe-naive --run-set swe-headline
$RUN --set swebench --config full --run-id swe-full --run-set swe-headline
eval/swebench/grade.sh eval/results/swe-single-shot
eval/swebench/grade.sh eval/results/swe-naive
eval/swebench/grade.sh eval/results/swe-full

$RUN --set custom --config single_shot --run-id custom-single-shot --run-set custom-headline
$RUN --set custom --config naive --run-id custom-naive --run-set custom-headline
$RUN --set custom --config full --run-id custom-full --run-set custom-headline
$RUN --set custom --config full --run-id custom-full-2 --run-set custom-headline

# Ablations, on a Haiku-class worker. The partner of each is a plain full run on the same model.
$RUN --set custom --config full --run-id abl-full --run-set ablations
$RUN --set custom --config full --context-mode no_compaction --run-id abl-no-compaction \\
  --run-set ablations
$RUN --set custom --config full --memory off --run-id abl-no-memory --run-set ablations

uv run python eval/chaos/chaos.py --runs 20 --seed 7
```

Then build the page. List the headline runs first, because the first plain run names the headline
model, and name the chaos folder that the suite printed:

```bash
uv run python eval/report/build_report.py swe-single-shot swe-naive swe-full \\
  custom-single-shot custom-naive custom-full custom-full-2 \\
  abl-full abl-no-compaction abl-no-memory chaos-DATE
```

The compaction ablation is paired on the tasks that both of its runs attempted. The runner has no
switch for the long-task subset yet, so until it has one the pairing covers every custom task.

`--allow-scripted` builds a page from runs that a script played. It adds a dry-run notice, and its
output says nothing about a model, so it is for trying the builder and not for this file.
"""


def placeholder_page() -> str:
    """The page for no runs: no number, and the commands that make the real one."""
    return PLACEHOLDER


def banner(eval_runs: Sequence[runs.EvalRun], chaos: Sequence[runs.ChaosRun]) -> str:
    scripted = [r.run_id for r in eval_runs if r.scripted] + [c.run_id for c in chaos if c.scripted]
    if not scripted:
        return ""
    total = len(eval_runs) + len(chaos)
    return (
        f"> **Dry run.** {len(scripted)} of {total} runs were played from scripts "
        "(`provider: scripted`), so no number on this page says anything about a model. The page "
        "exists to test the report."
    )


def image_refs(out: Path, img_dir: Path) -> dict[str, str]:
    """Where the page finds each chart, relative to the page."""
    base = Path(os.path.relpath(img_dir, out.parent)).as_posix()
    return {name: f"{base}/{name}" for name in charts.CHART_FILES}


def compose(
    loaded: runs.Loaded,
    order: Sequence[str],
    *,
    git_sha: str,
    refs: dict[str, str] | None,
) -> str:
    layout = runs.arrange(loaded.eval_runs)
    has_charts = bool(layout.swebench or layout.custom)
    parts = [
        "# Results",
        banner(loaded.eval_runs, loaded.chaos_runs),
        sections.method_section(layout),
        sections.swebench_section(layout),
        sections.custom_section(layout)
        + ("\n" + sections.rates_chart_section(refs) if has_charts else ""),
        sections.tokens_section(layout, refs),
        sections.ablation_section(layout),
        sections.chaos_section(loaded.chaos_runs),
        sections.unresolved_section(layout),
        sections.runs_section(loaded.eval_runs, loaded.chaos_runs, order, git_sha),
    ]
    return "\n\n".join(part.strip("\n") for part in parts if part) + "\n"


def build(
    run_ids: Sequence[str],
    *,
    results_dir: Path,
    out: Path,
    img_dir: Path,
    git_sha: str,
    allow_scripted: bool = False,
) -> str:
    """Write the page to `out` and the charts to `img_dir`, and return the page text."""
    if not run_ids:
        text = placeholder_page()
    else:
        loaded = runs.load_runs(results_dir, run_ids)
        scripted = [r.run_id for r in loaded.eval_runs if r.scripted]
        scripted += [c.run_id for c in loaded.chaos_runs if c.scripted]
        if scripted and not allow_scripted:
            raise runs.ReportError(
                f"these runs were played from scripts: {', '.join(scripted)}. Their numbers say "
                "nothing about a model, so they stay off the results page. Pass "
                "--allow-scripted to build a dry-run page from them."
            )
        layout = runs.arrange(loaded.eval_runs)
        refs = image_refs(out, img_dir) if charts.available() else None
        text = compose(loaded, run_ids, git_sha=git_sha, refs=refs)
        if refs is not None:
            charts.draw(layout, img_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return text


def current_git_sha() -> str:
    """HEAD of the checkout. A page without the commit it was built at could not be traced."""
    done = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False, cwd=ROOT
    )
    sha = done.stdout.strip()
    if done.returncode != 0 or not sha:
        raise runs.ReportError("cannot read the git sha of the checkout; pass --git-sha")
    return sha


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build docs/results.md from the result files of the given runs.",
    )
    parser.add_argument("run_ids", nargs="*", metavar="RUN_ID", help="folders under --results-dir")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--img-dir", type=Path, default=DEFAULT_IMG_DIR)
    parser.add_argument("--git-sha", help="for the footer; the default is HEAD of this checkout")
    parser.add_argument(
        "--allow-scripted",
        action="store_true",
        help="accept runs that a script played and mark the page as a dry run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sha = args.git_sha or (current_git_sha() if args.run_ids else "")
        build(
            args.run_ids,
            results_dir=args.results_dir,
            out=args.out,
            img_dir=args.img_dir,
            git_sha=sha,
            allow_scripted=args.allow_scripted,
        )
    except runs.ReportError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
