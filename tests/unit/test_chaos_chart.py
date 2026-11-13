"""scripts/chaos_chart.py: the chaos results chart that the README and the docs site show."""

import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = ROOT / "eval" / "results" / "chaos-2026-11-07" / "summary.json"
CHART = ROOT / "docs" / "media" / "chaos-results.svg"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "chaos_chart", ROOT / "scripts" / "chaos_chart.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["chaos_chart"] = module
    spec.loader.exec_module(module)
    return module


chaos_chart = _load()


def summary() -> dict[str, object]:
    data = json.loads(SUMMARY.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def texts(svg: str) -> list[str]:
    return [e.text.strip() for e in ET.fromstring(svg).iter() if e.text and e.text.strip()]


def test_the_committed_chart_is_drawn_from_the_summary() -> None:
    # Nobody draws the numbers by hand. Run `uv run python scripts/chaos_chart.py` after a new
    # chaos run and commit the file it writes.
    assert CHART.read_text(encoding="utf-8") == chaos_chart.render(summary())
    assert chaos_chart.main(["--check"]) == 0


def test_the_chart_labels_every_kill_type_with_the_numbers_of_the_summary() -> None:
    data = summary()
    found = texts(chaos_chart.render(data))
    landed, planned = data["kills_landed"], data["kills_planned"]
    latency = data["resume_latency_by_type"]
    assert isinstance(landed, dict) and isinstance(planned, dict) and isinstance(latency, dict)
    for kind, label in chaos_chart.KILL_TYPES:
        assert label in found
        assert f"{landed[kind]} / {planned[kind]}" in found
        row = latency[kind]
        assert f"{row['median_s']:.1f} s / {row['p95_s']:.1f} s" in found
    overall = data["resume_latency"]
    assert isinstance(overall, dict)
    assert f"{overall['median_s']:.1f} s / {overall['p95_s']:.1f} s" in found
    assert f"{sum(landed.values())} / {sum(planned.values())} in all" in found


def test_the_chart_says_the_answers_were_scripted() -> None:
    data = summary()
    assert set(data["recordings"].values()) == {"scripted"}  # type: ignore[attr-defined]
    title = texts(chaos_chart.render(data))[0]
    assert title.startswith(f"Chaos suite: {data['completed']} of {data['runs']} runs completed")
    assert "scripted recordings" in " ".join(texts(chaos_chart.render(data)))


def test_the_chart_follows_the_summary_when_it_changes() -> None:
    data = summary()
    data["kills_landed"] = {**data["kills_landed"], "worker": 7}  # type: ignore[dict-item]
    assert "7 / 10" in texts(chaos_chart.render(data))


def test_the_check_flag_fails_on_a_stale_file(tmp_path: Path) -> None:
    stale = tmp_path / "chart.svg"
    stale.write_text("<svg/>", encoding="utf-8")
    assert chaos_chart.main(["--check", "--out", str(stale)]) == 1
    assert chaos_chart.main(["--out", str(stale)]) == 0
    assert chaos_chart.main(["--check", "--out", str(stale)]) == 0


def test_the_chart_carries_light_and_dark_colors() -> None:
    svg = CHART.read_text(encoding="utf-8")
    assert "prefers-color-scheme: dark" in svg
    # The docs site embeds the chart inline and sets these variables from its own theme.
    for name in ("--chaos-surface", "--chaos-ink", "--chaos-mark"):
        assert re.search(rf"{name}:", svg), name
