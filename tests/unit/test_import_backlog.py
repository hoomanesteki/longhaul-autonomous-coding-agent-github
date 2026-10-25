import importlib.util
import json
import sys
from pathlib import Path

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
    phases, stories = backlog.parse_plan(PLAN.read_text(encoding="utf-8"))

    assert len(phases) == 10
    assert len(stories) == 80
    by_id = {story.id: story for story in stories}
    assert by_id["LH-001"].points == 3
    assert by_id["LH-001"].epic == "E0"
    assert "could" in backlog.story_labels(by_id["LH-908"])
    assert "could" in backlog.story_labels(by_id["LH-909"])
    assert sum(story.could for story in stories) == 2
    assert by_id["LH-102"].kind == "spike"
    assert all(story.theme in {f"T{n}" for n in range(1, 7)} for story in stories)


def test_idempotent_plan() -> None:
    gh = FakeGh()
    first = backlog.import_backlog(PLAN, "o/r", gh, dry_run=False, update=False)
    assert len(first) == 80
    assert len(gh.milestones) == 10
    assert len(gh.issues) == 80

    gh.calls.clear()
    second = backlog.import_backlog(PLAN, "o/r", gh, dry_run=False, update=False)

    creates = [
        call
        for call in gh.calls
        if "-X" in call or call[:2] in (["label", "create"], ["issue", "create"])
    ]
    assert creates == []
    assert second == first
    assert len(gh.issues) == 80


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
    gh = FakeGh()
    numbers = backlog.import_backlog(PLAN, None, gh, dry_run=True, update=False)

    assert gh.calls == []
    assert numbers == {}
    assert capsys.readouterr().out.splitlines()[-1] == "80 stories, 10 milestones"


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
