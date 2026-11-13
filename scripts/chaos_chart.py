"""Draw the chaos results chart from the summary file of a chaos run.

    uv run python scripts/chaos_chart.py            # writes docs/media/chaos-results.svg
    uv run python scripts/chaos_chart.py --check    # fails when the file is out of date

The chart shows the same numbers as the chaos table of the README: the kills that landed of
the kills planned, and the resume latency (median and 95th percentile) for each kill type.
Every number in it is read from the summary, so the picture cannot drift from the run. A test
draws it again and compares it with the committed file.

The SVG carries its own light and dark colors. On GitHub it follows the reader's color scheme,
and the docs site embeds it inline, where site/styles.css sets the colors from the site theme.
It uses only the standard library, so it also runs under a plain python3.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "eval" / "results" / "chaos-2026-11-07" / "summary.json"
OUTPUT = ROOT / "docs" / "media" / "chaos-results.svg"

# The kill types in the order of the README table, with the same labels.
KILL_TYPES = (
    ("worker", "Worker SIGKILL"),
    ("sandbox", "Sandbox kill"),
    ("temporal", "Temporal restart"),
    ("postgres", "Postgres restart"),
)

WIDTH = 880
LABEL_RIGHT = 142
BARS_LEFT, BARS_WIDTH = 158, 200
DOTS_LEFT, DOTS_WIDTH = 482, 290
FIRST_ROW, ROW_HEIGHT = 128, 34
BAR_HEIGHT = 14

STYLE = """\
svg.chaos-chart { --chaos-surface: #ffffff; --chaos-ink: #1f2933; --chaos-muted: #59626e;
  --chaos-grid: #dfe3e8; --chaos-track: #e3e7ec; --chaos-mark: #2a78d6; }
@media (prefers-color-scheme: dark) {
  svg.chaos-chart { --chaos-surface: #1b2028; --chaos-ink: #e3e8ef; --chaos-muted: #a3aebd;
    --chaos-grid: #2d343f; --chaos-track: #2d343f; --chaos-mark: #5b9cf0; }
}
svg.chaos-chart text { font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
svg.chaos-chart .surface { fill: var(--chaos-surface); }
svg.chaos-chart .title { fill: var(--chaos-ink); font-size: 17px; font-weight: 600; }
svg.chaos-chart .head { fill: var(--chaos-ink); font-size: 13px; font-weight: 600; }
svg.chaos-chart .label { fill: var(--chaos-ink); font-size: 13px; }
svg.chaos-chart .note { fill: var(--chaos-muted); font-size: 12px; }
svg.chaos-chart .grid { stroke: var(--chaos-grid); stroke-width: 1; }
svg.chaos-chart .track { fill: var(--chaos-track); }
svg.chaos-chart .bar { fill: var(--chaos-mark); }
svg.chaos-chart .range { stroke: var(--chaos-mark); stroke-width: 2; }
svg.chaos-chart .median { fill: var(--chaos-mark); }
svg.chaos-chart .p95 { fill: var(--chaos-surface); stroke: var(--chaos-mark); stroke-width: 2; }
"""


def seconds(value: float) -> str:
    return f"{value:.1f} s"


def _text(x: float, y: float, text: str, cls: str, anchor: str = "start") -> str:
    return f'<text x="{x:g}" y="{y:g}" class="{cls}" text-anchor="{anchor}">{escape(text)}</text>'


def _rect(x: float, y: float, width: float, height: float, cls: str) -> str:
    return (
        f'<rect x="{x:g}" y="{y:g}" width="{width:.1f}" height="{height:g}" rx="4" class="{cls}"/>'
    )


def render(summary: dict[str, Any]) -> str:
    """The whole SVG document for one chaos summary."""
    landed = summary["kills_landed"]
    planned = summary["kills_planned"]
    by_type = summary["resume_latency_by_type"]
    overall = summary["resume_latency"]
    runs = summary["runs"]
    rows = [(label, by_type[kind]) for kind, label in KILL_TYPES] + [("All kills", overall)]

    most_planned = max(planned[kind] for kind, _ in KILL_TYPES)
    longest = max(row["p95_s"] for _, row in rows)
    ceiling = max(10, int(math.ceil(longest / 10.0)) * 10)
    height = FIRST_ROW + ROW_HEIGHT * len(rows) + 46

    def bar_x(count: float) -> float:
        return BARS_LEFT + BARS_WIDTH * count / most_planned

    def dot_x(value: float) -> float:
        return DOTS_LEFT + DOTS_WIDTH * value / ceiling

    title = (
        f"Chaos suite: {summary['completed']} of {runs} runs completed, "
        f"{summary['diff_matches']} of {runs} with the same diff, "
        f"{summary['duplicate_side_effects']} duplicate writes"
    )
    note = (
        f"{summary['name']}, seed {summary['seed']}. "
        f"{sum(landed.values())} of {sum(planned.values())} planned kills landed. "
        "The model answers were scripted recordings."
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" class="chaos-chart" '
        f'viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" height="{height}" role="img" '
        f'aria-labelledby="chaos-chart-title chaos-chart-desc">',
        f'<title id="chaos-chart-title">{escape(title)}</title>',
        f'<desc id="chaos-chart-desc">{escape(note)} The README holds the same numbers '
        "as a table.</desc>",
        f"<style>\n{STYLE}</style>",
        f'<rect x="0" y="0" width="{WIDTH}" height="{height}" rx="10" class="surface"/>',
        _text(24, 36, title, "title"),
        _text(24, 60, note, "note"),
        _text(BARS_LEFT, 98, "Kills landed of planned", "head"),
        _text(DOTS_LEFT, 98, "Resume after a kill (seconds)", "head"),
    ]

    legend_y = 114
    parts += [
        f'<circle cx="{DOTS_LEFT + 5}" cy="{legend_y - 4}" r="5" class="median"/>',
        _text(DOTS_LEFT + 16, legend_y, "median", "note"),
        f'<circle cx="{DOTS_LEFT + 75}" cy="{legend_y - 4}" r="5" class="p95"/>',
        _text(DOTS_LEFT + 86, legend_y, "95th percentile", "note"),
    ]

    axis_top = FIRST_ROW + 6
    axis_bottom = FIRST_ROW + ROW_HEIGHT * len(rows) + 4
    for tick in range(0, ceiling + 1, 10):
        x = dot_x(tick)
        parts.append(
            f'<line x1="{x:g}" y1="{axis_top}" x2="{x:g}" y2="{axis_bottom}" class="grid"/>'
        )
        parts.append(_text(x, axis_bottom + 18, f"{tick} s", "note", "middle"))

    for index, (label, latency) in enumerate(rows):
        middle = FIRST_ROW + ROW_HEIGHT * index + ROW_HEIGHT / 2 + 4
        parts.append(_text(LABEL_RIGHT, middle + 4, label, "label", "end"))
        if index < len(KILL_TYPES):
            kind = KILL_TYPES[index][0]
            top = middle - BAR_HEIGHT / 2
            parts.append(
                _rect(BARS_LEFT, top, bar_x(planned[kind]) - BARS_LEFT, BAR_HEIGHT, "track")
            )
            parts.append(_rect(BARS_LEFT, top, bar_x(landed[kind]) - BARS_LEFT, BAR_HEIGHT, "bar"))
            parts.append(
                _text(
                    bar_x(planned[kind]) + 8,
                    middle + 4,
                    f"{landed[kind]} / {planned[kind]}",
                    "label",
                )
            )
        else:
            total = f"{sum(landed.values())} / {sum(planned.values())} in all"
            parts.append(_text(BARS_LEFT, middle + 4, total, "label"))
        low, high = dot_x(latency["median_s"]), dot_x(latency["p95_s"])
        parts.append(
            f'<line x1="{low:.1f}" y1="{middle:g}" x2="{high:.1f}" y2="{middle:g}" class="range"/>'
        )
        parts.append(f'<circle cx="{high:.1f}" cy="{middle:g}" r="5" class="p95"/>')
        parts.append(f'<circle cx="{low:.1f}" cy="{middle:g}" r="5" class="median"/>')
        values = f"{seconds(latency['median_s'])} / {seconds(latency['p95_s'])}"
        parts.append(_text(high + 12, middle + 4, values, "label"))

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--summary", type=Path, default=SUMMARY)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail when the file is stale")
    args = parser.parse_args(argv)

    chart = render(json.loads(args.summary.read_text(encoding="utf-8")))
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != chart:
            print(f"chaos_chart: {args.out} is out of date, run scripts/chaos_chart.py")
            return 1
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(chart, encoding="utf-8")
    print(f"chaos_chart: wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
