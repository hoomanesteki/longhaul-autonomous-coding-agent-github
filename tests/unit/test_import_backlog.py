import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "docs" / "plan" / "longhaul-build-plan.md"

_SPEC = importlib.util.spec_from_file_location(
    "import_backlog", ROOT / "scripts" / "import_backlog.py"
)
assert _SPEC and _SPEC.loader
backlog = importlib.util.module_from_spec(_SPEC)
# dataclasses look the module up in sys.modules, so register it before running it.
sys.modules["import_backlog"] = backlog
_SPEC.loader.exec_module(backlog)


# A fenced block, matched by its own opening marker so a ```` block can hold ``` lines.
FENCED_RE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)
PHASE_HEADING_RE = re.compile(r"^## Phase (\d): (.+) \(tag v[\d.]+\)$", re.MULTILINE)
STORY_HEADING_RE = re.compile(r"^#### (LH-\d{3}) · ", re.MULTILINE)

# What the gatekeeper appends to a phase when its gate fails (plan section 5.4).
FIX_STORY = """\
#### {id} · Fix unit coverage below the CI floor
- **Epic:** E0 · **Points:** 2 · **Branch:** `fix/{slug}-unit-coverage` · **Depends on:** LH-008

"""


def _plan_headings(text: str) -> tuple[list[tuple[int, str]], list[str]]:
    """Read the phase and story headings with regexes, without the importer's parser."""
    prose = FENCED_RE.sub("", text)
    phases = [(int(number), name) for number, name in PHASE_HEADING_RE.findall(prose)]
    return phases, STORY_HEADING_RE.findall(prose)


def _is_fix(story: Any) -> bool:
    # Gate fix stories sit on fix/ branches and are numbered LH-N91 and up (plan 5.2, 5.4).
    return bool(story.kind == "fix" or int(story.id[-2:]) >= 91)


def _with_fix_story(text: str) -> tuple[str, str]:
    """Append a fix story at the end of phase 0, just before the phase 1 heading.

    The id is the highest free LH-0nn, so the helper still works once a real fix story
    has been added to the plan.
    """
    _, used = _plan_headings(text)
    fix_id = next(f"LH-{n:03d}" for n in range(99, 0, -1) if f"LH-{n:03d}" not in used)
    marker = "## Phase 1:"
    assert text.count("\n" + marker) == 1
    block = FIX_STORY.format(id=fix_id, slug=fix_id.lower())
    return text.replace("\n" + marker, "\n" + block + marker, 1), fix_id


def _value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


class FakeGh:
    """Stands in for `gh`. Anything it creates shows up in later list calls."""

    def __init__(self) -> None:
        self.milestones: list[dict[str, object]] = []
        self.labels: list[str] = []
        self.issues: list[dict[str, object]] = []
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "api":
            if "-X" in args:
                title = _value(args, "-f").removeprefix("title=")
                self.milestones.append({"title": title, "number": len(self.milestones) + 1})
                return json.dumps(self.milestones[-1])
            return json.dumps(self.milestones)
        if args[:2] == ["label", "list"]:
            return json.dumps([{"name": name} for name in self.labels])
        if args[:2] == ["label", "create"]:
            self.labels.append(args[2])
            return ""
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.issues)
        if args[:2] == ["issue", "create"]:
            number = len(self.issues) + 1
            self.issues.append(
                {
                    "number": number,
                    "title": _value(args, "--title"),
                    "body": _value(args, "--body"),
                }
            )
            return f"https://github.com/o/r/issues/{number}\n"
        if args[:2] == ["issue", "edit"]:
            number = int(args[2])
            for issue in self.issues:
                if issue["number"] == number:
                    issue["body"] = _value(args, "--body")
            return ""
        raise AssertionError(f"unexpected gh call: {args}")


def test_parses_all_stories() -> None:
    plan_text = PLAN.read_text(encoding="utf-8")
    phases, stories = backlog.parse_plan(plan_text)

    # Expected counts come from the plan, so a story added by a gate does not break this.
    heading_phases, heading_ids = _plan_headings(plan_text)
    assert [(phase.number, phase.name) for phase in phases] == heading_phases
    assert [story.id for story in stories] == heading_ids
    by_id = {story.id: story for story in stories}
    assert by_id["LH-001"].points == 3
    assert by_id["LH-001"].epic == "E0"
    assert "could" in backlog.story_labels(by_id["LH-908"])
    assert "could" in backlog.story_labels(by_id["LH-909"])
    assert by_id["LH-102"].kind == "spike"
    assert all(story.theme in {f"T{n}" for n in range(1, 7)} for story in stories)


def test_v1_plan_size_ignores_fix_stories() -> None:
    phases, stories = backlog.parse_plan(PLAN.read_text(encoding="utf-8"))
    v1_stories = [story for story in stories if not _is_fix(story)]

    # The size the v1 plan was written with (plan section 6.3). Fix stories come on top.
    assert len(v1_stories) == 80
    assert len(phases) == 10
    assert sum(story.could for story in v1_stories) == 2


def test_gate_fix_story_adds_one_story_and_no_milestone(tmp_path: Path) -> None:
    base_phases, base_stories = backlog.parse_plan(PLAN.read_text(encoding="utf-8"))
    text, fix_id = _with_fix_story(PLAN.read_text(encoding="utf-8"))
    plan = tmp_path / "plan.md"
    plan.write_text(text, encoding="utf-8")

    phases, stories = backlog.parse_plan(plan.read_text(encoding="utf-8"))
    fix = next(story for story in stories if story.id == fix_id)

    assert len(phases) == len(base_phases)
    assert len(stories) == len(base_stories) + 1
    assert (fix.kind, fix.phase) == ("fix", 0)
    assert backlog.story_labels(fix) == ["phase:0", "epic:E0", "pts:2", "theme:T5", "type:fix"]
    assert [story for story in stories if not _is_fix(story)] == [
        story for story in base_stories if not _is_fix(story)
    ]

    gh = FakeGh()
    numbers = backlog.import_backlog(plan, "o/r", gh)
    assert len(numbers) == len(gh.issues) == len(stories)
    assert len(gh.milestones) == len(phases)


def test_idempotent_plan() -> None:
    phases, stories = backlog.parse_plan(PLAN.read_text(encoding="utf-8"))
    gh = FakeGh()
    first = backlog.import_backlog(PLAN, "o/r", gh, dry_run=False, update=False)
    assert len(first) == len(stories)
    assert len(gh.milestones) == len(phases)
    assert len(gh.issues) == len(stories)

    gh.calls.clear()
    second = backlog.import_backlog(PLAN, "o/r", gh, dry_run=False, update=False)

    creates = [
        call
        for call in gh.calls
        if "-X" in call or call[:2] in (["label", "create"], ["issue", "create"])
    ]
    assert creates == []
    assert second == first
    assert len(gh.issues) == len(stories)


def test_malformed_heading_raises() -> None:
    bad = (
        "## Phase 0: Foundation (tag v0.0.1)\n"
        "\n"
        "#### LH-001 · Fine\n"
        "- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/x` · **Depends on:** none\n"
        "\n"
        "#### LH-12 · Bad\n"
    )
    with pytest.raises(ValueError, match="line 6"):
        backlog.parse_plan(bad)

    # A ```` block that contains ``` lines is code, so a heading inside it is ignored.
    fenced = (
        "## Phase 0: Foundation (tag v0.0.1)\n\n````markdown\n```\n#### LH-12 · Bad\n```\n````\n"
    )
    phases, stories = backlog.parse_plan(fenced)
    assert len(phases) == 1
    assert stories == []


def test_dry_run_makes_no_calls(capsys: pytest.CaptureFixture[str]) -> None:
    phases, stories = backlog.parse_plan(PLAN.read_text(encoding="utf-8"))
    gh = FakeGh()
    numbers = backlog.import_backlog(PLAN, None, gh, dry_run=True, update=False)

    assert gh.calls == []
    assert numbers == {}
    summary = f"{len(stories)} stories, {len(phases)} milestones"
    assert capsys.readouterr().out.splitlines()[-1] == summary


def test_writes_issue_column_and_keeps_ids(tmp_path: Path) -> None:
    progress = tmp_path / "progress.md"
    progress.write_text(
        "# Progress\n"
        "\n"
        "## Phase 0: Foundation (tag v0.0.1)\n"
        "| ID | Title | Pts | Status | PR | Merged |\n"
        "|---|---|---|---|---|---|\n"
        "| LH-001 | Repo scaffold | 3 | Todo | | |\n"
        "| LH-002 | Local stack | 3 | Todo | | |\n",
        encoding="utf-8",
    )
    backlog.write_issue_numbers(progress, {"LH-001": 7})
    first = progress.read_text(encoding="utf-8")
    assert "| ID | Title | Pts | Status | PR | Merged | Issue |" in first
    assert "| LH-001 | Repo scaffold | 3 | Todo | | | #7 |" in first
    assert "| LH-002 | Local stack | 3 | Todo | | | |" in first

    backlog.write_issue_numbers(progress, {"LH-002": 8})
    second = progress.read_text(encoding="utf-8")
    assert second.count("| Issue |") == 1
    assert "| LH-001 | Repo scaffold | 3 | Todo | | | #7 |" in second
    assert "| LH-002 | Local stack | 3 | Todo | | | #8 |" in second
