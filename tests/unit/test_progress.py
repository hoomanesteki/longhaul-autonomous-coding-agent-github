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
PROGRESS_PHASE_RE = re.compile(r"^## Phase (\d):")

# The shapes a gate fix story takes (plan section 5.4): a story block in the plan and
# a matching row in the phase table of progress.md.
SAMPLE_PLAN = """\
## Phase 0: Foundation (tag v0.0.1)

#### LH-001 · Scaffold
- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-001-scaffold` · **Depends on:** none

```text
#### LH-002 · Inside a fence, so not a story
```

## Phase 1: Core (tag v0.1.0)

#### LH-101 · Contracts
- **Epic:** E1 · **Points:** 3 · **Branch:** `feat/lh-101-contracts` · **Depends on:** LH-001
"""
SAMPLE_PROGRESS = """\
# Progress

## Phase 0: Foundation (tag v0.0.1)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-001 | Scaffold | 3 | Done | #1 | 2026-10-25 |

## Phase 1: Core (tag v0.1.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-101 | Contracts | 3 | Todo | | |
"""
FIX_STORY = """\
#### LH-191 · Fix contract drift
- **Epic:** E1 · **Points:** 2 · **Branch:** `fix/lh-191-contract-drift` · **Depends on:** LH-101
"""
FIX_ROW = "| LH-191 | Fix contract drift | 2 | Todo | | |\n"


def _plan_story_ids(text: str) -> list[str]:
    ids: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = STORY_HEADING_RE.match(line)
        if match:
            ids.append(match.group(1))
    return ids


def _progress_story_ids(text: str) -> list[str]:
    return [match.group(1) for line in text.splitlines() if (match := PROGRESS_ROW_RE.match(line))]


def _rows_in_wrong_table(text: str) -> list[str]:
    wrong: list[str] = []
    phase: int | None = None
    for line in text.splitlines():
        heading = PROGRESS_PHASE_RE.match(line)
        if heading:
            phase = int(heading.group(1))
            continue
        row = PROGRESS_ROW_RE.match(line)
        # The hundreds digit of a story id is its phase, fix stories included.
        if row and int(row.group(1)[3]) != phase:
            wrong.append(row.group(1))
    return wrong


def check_progress_matches_plan(plan_text: str, progress_text: str) -> None:
    """Compare the two id sets. No fixed total, because gate fix stories add to both."""
    plan_ids = _plan_story_ids(plan_text)
    assert plan_ids, "no story headings found in the plan"
    assert len(plan_ids) == len(set(plan_ids)), "duplicate story headings in the plan"

    progress_ids = _progress_story_ids(progress_text)
    assert len(progress_ids) == len(set(progress_ids)), "duplicate rows in progress.md"
    missing = sorted(set(plan_ids) - set(progress_ids))
    extra = sorted(set(progress_ids) - set(plan_ids))
    assert not missing, f"stories with no progress row: {missing}"
    assert not extra, f"progress rows with no story in the plan: {extra}"


def test_progress_lists_all_stories() -> None:
    check_progress_matches_plan(
        PLAN.read_text(encoding="utf-8"), PROGRESS.read_text(encoding="utf-8")
    )


def test_progress_rows_sit_in_their_phase_table() -> None:
    assert _rows_in_wrong_table(PROGRESS.read_text(encoding="utf-8")) == []


def test_sample_files_match() -> None:
    check_progress_matches_plan(SAMPLE_PLAN, SAMPLE_PROGRESS)
    assert _rows_in_wrong_table(SAMPLE_PROGRESS) == []


def test_fix_story_added_to_both_files_still_matches() -> None:
    plan = SAMPLE_PLAN.replace("## Phase 1:", FIX_STORY + "\n## Phase 1:")
    progress = SAMPLE_PROGRESS + FIX_ROW
    check_progress_matches_plan(plan, progress)


def test_fix_story_missing_from_progress_is_reported() -> None:
    plan = SAMPLE_PLAN.replace("## Phase 1:", FIX_STORY + "\n## Phase 1:")
    with pytest.raises(AssertionError, match=r"no progress row: \['LH-191'\]"):
        check_progress_matches_plan(plan, SAMPLE_PROGRESS)


def test_progress_row_without_a_story_is_reported() -> None:
    with pytest.raises(AssertionError, match=r"no story in the plan: \['LH-191'\]"):
        check_progress_matches_plan(SAMPLE_PLAN, SAMPLE_PROGRESS + FIX_ROW)


def test_duplicate_rows_are_reported() -> None:
    with pytest.raises(AssertionError, match="duplicate rows"):
        check_progress_matches_plan(SAMPLE_PLAN, SAMPLE_PROGRESS + FIX_ROW + FIX_ROW)


def test_row_under_the_wrong_phase_is_reported() -> None:
    # LH-191 belongs to phase 1, but this row sits in the phase 0 table.
    progress = SAMPLE_PROGRESS.replace("\n## Phase 1:", FIX_ROW + "\n## Phase 1:")
    assert _rows_in_wrong_table(progress) == ["LH-191"]
