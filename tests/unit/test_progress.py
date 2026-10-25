import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "plan" / "longhaul-build-plan.md"
PROGRESS = ROOT / "docs" / "progress.md"

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
STORY_HEADING_RE = re.compile(r"^#### (LH-\d{3}) · ")
PROGRESS_ROW_RE = re.compile(r"^\| (LH-\d{3}) \|")


def _plan_story_ids() -> list[str]:
    ids: list[str] = []
    in_fence = False
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = STORY_HEADING_RE.match(line)
        if match:
            ids.append(match.group(1))
    return ids


def _progress_story_ids() -> list[str]:
    return [
        match.group(1)
        for line in PROGRESS.read_text(encoding="utf-8").splitlines()
        if (match := PROGRESS_ROW_RE.match(line))
    ]


def test_progress_lists_all_stories() -> None:
    plan_ids = _plan_story_ids()
    assert len(plan_ids) == len(set(plan_ids)) == 80

    progress_ids = _progress_story_ids()
    assert len(progress_ids) == len(set(progress_ids)), "duplicate rows in progress.md"
    assert len(progress_ids) == 80
    assert set(progress_ids) == set(plan_ids)
