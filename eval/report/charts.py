"""The two charts of the results report, drawn with matplotlib (LH-905).

matplotlib is in the `eval` extra and not in the base install, so CI does not have it. The
builder asks `available()` first and writes the page without images when it is missing, with a
line that says how to get them. The Agg backend draws to files and needs no display.

A chart holds the same numbers as a table of the page: the box plot is the tokens-per-task
table, and the interval chart is the resolve-rate columns of tables 1 and 2.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

from eval.report import stats
from eval.report.runs import Layout
from eval.report.sections import labelled, tokens_of

if TYPE_CHECKING:
    from matplotlib.figure import Figure

TOKENS_FILE = "tokens-per-task.png"
RATES_FILE = "resolve-rates.png"
CHART_FILES = (TOKENS_FILE, RATES_FILE)
SET_SHORT = {"swebench": "SWE-bench", "custom": "custom"}


def available() -> bool:
    return importlib.util.find_spec("matplotlib") is not None


def _groups(layout: Layout) -> list[tuple[str, list[float], int, int]]:
    """(label, tokens per task, resolved, attempted) for each run of the headline tables."""
    found = []
    for by_config in (layout.swebench, layout.custom):
        for label, run in labelled(by_config):
            found.append(
                (
                    f"{SET_SHORT[run.task_set]}\n{label}",
                    [float(tokens_of(r)) for r in run.results],
                    run.resolved,
                    run.attempted,
                )
            )
    return found


def draw(layout: Layout, img_dir: Path) -> list[Path]:
    """Write both charts into `img_dir`. The caller checks `available()` first."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    groups = _groups(layout)
    if not groups:
        return []
    img_dir.mkdir(parents=True, exist_ok=True)
    written = []

    figure, axes = plt.subplots(figsize=(max(5.0, 1.3 * len(groups)), 4.2))
    axes.boxplot([g[1] for g in groups], tick_labels=[g[0] for g in groups], showfliers=True)
    axes.set_ylabel("tokens per task (input + output + cache reads)")
    axes.set_title("Tokens per task")
    axes.grid(axis="y", alpha=0.3)
    written.append(_save(figure, img_dir / TOKENS_FILE))

    figure, axes = plt.subplots(figsize=(max(5.0, 1.3 * len(groups)), 4.2))
    positions = range(len(groups))
    rates = [100 * g[2] / g[3] for g in groups]
    bounds = [stats.wilson(g[2], g[3]) for g in groups]
    below = [rate - 100 * low for rate, (low, _) in zip(rates, bounds, strict=True)]
    above = [100 * high - rate for rate, (_, high) in zip(rates, bounds, strict=True)]
    axes.errorbar(positions, rates, yerr=[below, above], fmt="o", capsize=5)
    axes.set_xticks(list(positions), [g[0] for g in groups])
    axes.set_ylim(0, 100)
    axes.set_ylabel("resolved (%), with 95% Wilson interval")
    axes.set_title("Resolve rate")
    axes.grid(axis="y", alpha=0.3)
    written.append(_save(figure, img_dir / RATES_FILE))
    return written


def _save(figure: Figure, path: Path) -> Path:
    import matplotlib.pyplot as plt

    figure.tight_layout()
    # No software version in the file, so the same numbers give the same bytes.
    figure.savefig(path, dpi=130, metadata={"Software": None})
    plt.close(figure)
    return path
