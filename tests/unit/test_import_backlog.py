import importlib.util
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

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
REAL_GH_RUNNER = backlog.GhRunner


# A fenced block, matched by its own opening marker so a ```` block can hold ``` lines.
FENCED_RE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)
PHASE_HEADING_RE = re.compile(r"^## Phase (\d): (.+) \(tag v[\d.]+\)$", re.MULTILINE)
STORY_HEADING_RE = re.compile(r"^#### (LH-\d{3}) · (.+)$", re.MULTILINE)

# The labels GitHub puts on every new repository.
DEFAULT_LABELS = (
    "bug",
    "documentation",
    "duplicate",
    "enhancement",
    "good first issue",
    "help wanted",
    "invalid",
    "question",
    "wontfix",
)

# A small plan with one of each kind of story, for tests that check exact output.
MINI_PLAN = """\
# Mini plan

### 6.2 Epics

| Epic | Name | Theme | Phases | Points |
|---|---|---|---|---|
| E0 | Repo foundation | T5 | 0 | 6 |
| E1 | LLM providers | T1 | 1 | 7 |

## Phase 0: Foundation (tag v0.0.1)

#### LH-001 · Repo scaffold
- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-001-scaffold` · **Depends on:** none
- **Story:** As a developer, I want a scaffold.

```bash
#### LH-050 · A heading inside a fence is not a story
```

#### LH-002 · Local stack
- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-002-local-stack` · **Depends on:** LH-001
- **Story:** As a developer, I want a local stack.

## Phase 1: Agent core (tag v0.1.0)

#### LH-102 · Spike: tool use
- **Epic:** E1 · **Points:** 2 · **Branch:** `spike/lh-102-tool-use` · **Depends on:** none
- **Story:** As a developer, I want to know how tool use works.

#### LH-109 · Extras (Could)
- **Epic:** E1 · **Points:** 5 · **Branch:** `feat/lh-109-extras` · **Depends on:** LH-102
- **Story:** As a developer, I want extras.

### Integration checkpoint 1

- **Cumulative tests:** `make gate PHASE=1`.
"""
MINI_PROGRESS = """\
# Progress

## Phase 0: Foundation (tag v0.0.1)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-001 | Repo scaffold | 3 | Todo | | |
| LH-002 | Local stack | 3 | Todo | | |

## Phase 1: Agent core (tag v0.1.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-102 | Spike: tool use | 2 | Todo | | |
| LH-109 | Extras | 5 | Todo | | |
"""
STANDARD_LABELS = ["phase:0", "epic:E0", "pts:3", "theme:T5", "type:story"]

# What the gatekeeper appends to a phase when its gate fails (plan section 5.4).
FIX_STORY = """\
#### {id} · Fix unit coverage below the CI floor
- **Epic:** E0 · **Points:** 2 · **Branch:** `fix/{slug}-unit-coverage` · **Depends on:** LH-008

"""


def _plan_headings(text: str) -> tuple[list[tuple[int, str]], list[tuple[str, str]]]:
    """Read the phase and story headings with regexes, without the importer's parser."""
    prose = FENCED_RE.sub("", text)
    phases = [(int(number), name) for number, name in PHASE_HEADING_RE.findall(prose)]
    return phases, STORY_HEADING_RE.findall(prose)


def _is_fix(story: Any) -> bool:
    # Gate fix stories sit on fix/ branches and are numbered LH-N91 and up (plan 5.2, 5.4).
    return bool(story.kind == "fix" or int(story.id[-2:]) >= 91)


def _add_fix_story(text: str, fix_id: str) -> str:
    """Append a fix story at the end of phase 0, just before the phase 1 heading."""
    marker = "## Phase 1:"
    assert text.count("\n" + marker) == 1
    block = FIX_STORY.format(id=fix_id, slug=fix_id.lower())
    return text.replace("\n" + marker, "\n" + block + marker, 1)


def _with_fix_story(text: str) -> tuple[str, str]:
    """Add a fix story to a plan under the highest free LH-0nn id.

    A free id keeps this working once a real fix story has been added to the plan.
    """
    _, used = _plan_headings(text)
    used_ids = {story_id for story_id, _ in used}
    fix_id = next(f"LH-{n:03d}" for n in range(99, 0, -1) if f"LH-{n:03d}" not in used_ids)
    return _add_fix_story(text, fix_id), fix_id


def _write(tmp_path: Path, text: str, name: str = "plan.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _split_labels(values: list[str]) -> list[str]:
    return [name for value in values for name in value.split(",")]


def _parse_flags(
    rest: list[str], allowed: set[str], booleans: frozenset[str] = frozenset()
) -> tuple[list[str], dict[str, list[str]]]:
    """Split gh arguments into positionals and flags. Like gh, refuse a flag it lacks."""
    positional: list[str] = []
    flags: dict[str, list[str]] = {}
    index = 0
    while index < len(rest):
        arg = rest[index]
        if not arg.startswith("-"):
            positional.append(arg)
            index += 1
        elif arg not in allowed:
            raise backlog.GhError(f"unknown flag: {arg}")
        elif arg in booleans:
            flags.setdefault(arg, []).append("true")
            index += 1
        elif index + 1 >= len(rest):
            raise backlog.GhError(f"flag needs an argument: {arg}")
        else:
            flags.setdefault(arg, []).append(rest[index + 1])
            index += 2
    return positional, flags


class FakeGh:
    """Stands in for `gh` and refuses what the real CLI refuses.

    Every call needs the repo, because real gh would otherwise use the repository of the
    working directory. `--label` needs labels that exist and `--milestone` needs an open
    milestone. Lists honour `--state` and `--limit`, whose defaults are open and 30. A repo
    with no labels prints nothing for `label list`, not `[]`. Bodies are stored as sent.
    """

    def __init__(self, repo: str = "o/r", labels: Sequence[str] = DEFAULT_LABELS) -> None:
        self.repo = repo
        self.labels = list(labels)
        self.milestones: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.calls: list[list[str]] = []

    def writes(self) -> list[list[str]]:
        """The calls that change something on GitHub."""
        return [
            call
            for call in self.calls
            if call[:2] in (["label", "create"], ["issue", "create"], ["issue", "edit"])
            or (call[0] == "api" and "-X" in call)
        ]

    def issue(self, story_id: str) -> dict[str, Any]:
        return next(issue for issue in self.issues if issue["title"].startswith(f"{story_id}: "))

    def run(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "api":
            return self._api(args[1:])
        handlers: dict[tuple[str, str], Callable[[list[str]], str]] = {
            ("repo", "view"): self._repo_view,
            ("label", "list"): self._label_list,
            ("label", "create"): self._label_create,
            ("issue", "list"): self._issue_list,
            ("issue", "create"): self._issue_create,
            ("issue", "edit"): self._issue_edit,
        }
        handler = handlers.get((args[0], args[1]))
        if handler is None:
            raise AssertionError(f"unexpected gh call: {args}")
        return handler(args[2:])

    def _check_repo(self, flags: dict[str, list[str]]) -> None:
        if "--repo" not in flags:
            raise AssertionError("gh call without --repo would use the working directory's repo")
        if flags["--repo"] != [self.repo]:
            raise backlog.GhError(f"Could not resolve to a Repository: {flags['--repo'][0]}")

    def _require_labels(self, names: list[str]) -> None:
        for name in names:
            if name not in self.labels:
                raise backlog.GhError(f"could not add label: '{name}' not found")

    def _require_open_milestone(self, title: str) -> None:
        if not any(m["title"] == title and m["state"] == "open" for m in self.milestones):
            raise backlog.GhError(f"could not add to milestone '{title}': '{title}' not found")

    def _api(self, rest: list[str]) -> str:
        positional, flags = _parse_flags(rest, {"-X", "-f"})
        endpoint = urlsplit(positional[0])
        prefix = f"repos/{self.repo}/"
        if not endpoint.path.startswith(prefix):
            raise AssertionError(f"gh api call outside {prefix}: {positional[0]}")
        method = flags["-X"][0] if "-X" in flags else ("POST" if "-f" in flags else "GET")
        fields = dict(item.split("=", 1) for item in flags.get("-f", []))
        resource = endpoint.path.removeprefix(prefix)
        if (method, resource) == ("GET", "milestones"):
            return self._milestone_list(parse_qs(endpoint.query))
        if (method, resource) == ("POST", "milestones"):
            return self._milestone_create(fields)
        if method == "PATCH" and resource.startswith("milestones/"):
            return self._milestone_patch(int(resource.removeprefix("milestones/")), fields)
        raise AssertionError(f"unexpected gh api call: {rest}")

    def _milestone_list(self, query: dict[str, list[str]]) -> str:
        state = query.get("state", ["open"])[0]
        per_page = int(query.get("per_page", ["30"])[0])
        rows = [m for m in self.milestones if state == "all" or m["state"] == state]
        return json.dumps(rows[:per_page])

    def _milestone_create(self, fields: dict[str, str]) -> str:
        if any(m["title"] == fields["title"] for m in self.milestones):
            raise backlog.GhError("gh: Validation Failed (HTTP 422)")
        milestone = {"number": len(self.milestones) + 1, "title": fields["title"], "state": "open"}
        self.milestones.append(milestone)
        return json.dumps(milestone)

    def _milestone_patch(self, number: int, fields: dict[str, str]) -> str:
        milestone = next((m for m in self.milestones if m["number"] == number), None)
        if milestone is None:
            raise backlog.GhError("gh: Not Found (HTTP 404)")
        assert set(fields) == {"state"} and fields["state"] in {"open", "closed"}
        milestone["state"] = fields["state"]
        return json.dumps(milestone)

    def _repo_view(self, rest: list[str]) -> str:
        _, flags = _parse_flags(rest, {"--json", "-q"})
        assert flags == {"--json": ["nameWithOwner"], "-q": [".nameWithOwner"]}
        return self.repo + "\n"

    def _label_list(self, rest: list[str]) -> str:
        _, flags = _parse_flags(rest, {"--repo", "--limit", "--json"})
        self._check_repo(flags)
        assert flags["--json"] == ["name"]
        names = self.labels[: int(flags.get("--limit", ["30"])[0])]
        return json.dumps([{"name": name} for name in names]) if names else ""

    def _label_create(self, rest: list[str]) -> str:
        positional, flags = _parse_flags(rest, {"--repo", "--force"}, frozenset({"--force"}))
        self._check_repo(flags)
        (name,) = positional
        if name not in self.labels:
            self.labels.append(name)
        elif "--force" not in flags:
            raise backlog.GhError(f'label with name "{name}" already exists; use `--force`')
        return ""

    def _issue_json(self, issue: dict[str, Any], fields: list[str]) -> dict[str, Any]:
        milestone = next((m for m in self.milestones if m["title"] == issue["milestone"]), None)
        full = {
            "number": issue["number"],
            "title": issue["title"],
            "body": issue["body"],
            "labels": [{"name": name} for name in issue["labels"]],
            "milestone": None
            if milestone is None
            else {"number": milestone["number"], "title": milestone["title"]},
        }
        unknown = sorted(set(fields) - set(full))
        if unknown:
            raise backlog.GhError(f"Unknown JSON field: {unknown}")
        return {field: full[field] for field in fields}

    def _issue_list(self, rest: list[str]) -> str:
        _, flags = _parse_flags(rest, {"--repo", "--state", "--limit", "--json"})
        self._check_repo(flags)
        state = flags.get("--state", ["open"])[0]
        assert state in {"open", "closed", "all"}
        limit = int(flags.get("--limit", ["30"])[0])
        fields = flags["--json"][0].split(",")
        newest_first = reversed(self.issues)
        rows = [i for i in newest_first if state == "all" or i["state"] == state][:limit]
        return json.dumps([self._issue_json(issue, fields) for issue in rows])

    def _issue_create(self, rest: list[str]) -> str:
        allowed = {"--repo", "--title", "--body", "--label", "--milestone"}
        _, flags = _parse_flags(rest, allowed)
        self._check_repo(flags)
        if "--title" not in flags or "--body" not in flags:
            raise backlog.GhError("`--title` and `--body` are required when not interactive")
        labels = _split_labels(flags.get("--label", []))
        self._require_labels(labels)
        milestone = flags["--milestone"][0] if "--milestone" in flags else None
        if milestone is not None:
            self._require_open_milestone(milestone)
        number = len(self.issues) + 1
        self.issues.append(
            {
                "number": number,
                "title": flags["--title"][0],
                "body": flags["--body"][0],
                "labels": labels,
                "milestone": milestone,
                "state": "open",
            }
        )
        return f"https://github.com/{self.repo}/issues/{number}\n"

    def _issue_edit(self, rest: list[str]) -> str:
        allowed = {"--repo", "--title", "--body", "--add-label", "--remove-label", "--milestone"}
        positional, flags = _parse_flags(rest, allowed)
        self._check_repo(flags)
        if set(flags) == {"--repo"}:
            raise backlog.GhError("specify at least one property to edit")
        (number,) = positional
        issue = next((i for i in self.issues if i["number"] == int(number)), None)
        if issue is None:
            raise backlog.GhError(f"Could not resolve to an issue with the number of {number}")
        added = _split_labels(flags.get("--add-label", []))
        removed = _split_labels(flags.get("--remove-label", []))
        self._require_labels(added)
        if "--milestone" in flags:
            self._require_open_milestone(flags["--milestone"][0])
        for field, flag in (("title", "--title"), ("body", "--body"), ("milestone", "--milestone")):
            if flag in flags:
                issue[field] = flags[flag][0]
        kept = [name for name in issue["labels"] if name not in removed]
        issue["labels"] = kept + [name for name in added if name not in kept]
        return f"https://github.com/{self.repo}/issues/{number}\n"


def test_parses_all_stories() -> None:
    plan_text = PLAN.read_text(encoding="utf-8")
    phases, stories = backlog.parse_plan(plan_text)

    # Expected counts come from the plan, so a story added by a gate does not break this.
    heading_phases, heading_stories = _plan_headings(plan_text)
    assert [(phase.number, phase.name) for phase in phases] == heading_phases
    assert [story.id for story in stories] == [story_id for story_id, _ in heading_stories]
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
    plan = _write(tmp_path, text)

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


def test_each_branch_prefix_gives_its_story_kind() -> None:
    text = _add_fix_story(MINI_PLAN, "LH-091")
    _, stories = backlog.parse_plan(text)

    assert [(story.id, story.kind, story.could) for story in stories] == [
        ("LH-001", "story", False),
        ("LH-002", "story", False),
        ("LH-091", "fix", False),
        ("LH-102", "spike", False),
        ("LH-109", "story", True),
    ]
    fix = stories[2]
    assert backlog.story_labels(fix) == ["phase:0", "epic:E0", "pts:2", "theme:T5", "type:fix"]


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


def _line_of(text: str, needle: str) -> int:
    return next(n for n, line in enumerate(text.splitlines(), start=1) if needle in line)


# Plans a person could break by hand just before the one-shot import.
BROKEN_PLANS = [
    pytest.param(
        MINI_PLAN.replace("(tag v0.1.0)", "(tag 0.1.0)"),
        "(tag 0.1.0)",
        "line {line}: malformed phase heading",
        id="mistyped-phase-heading",
    ),
    pytest.param(
        MINI_PLAN.replace("## Phase 1:", "## Phase 2:"),
        "## Phase 2:",
        "line {line}: found Phase 2 where Phase 1 should come next",
        id="phase-number-skipped",
    ),
    pytest.param(
        MINI_PLAN.replace("## Phase 1:", "## Phase 0:"),
        "## Phase 0: Agent core",
        "line {line}: found Phase 0 where Phase 1 should come next",
        id="phase-number-repeated",
    ),
    pytest.param(
        MINI_PLAN.replace("#### LH-109 ·", "#### LH-209 ·"),
        "#### LH-209",
        "line {line}: LH-209 sits under Phase 1, but its id says phase 2",
        id="story-under-the-wrong-phase",
    ),
]


@pytest.mark.parametrize(("text", "anchor", "message"), BROKEN_PLANS)
def test_a_broken_plan_fails_before_any_gh_call(
    tmp_path: Path, text: str, anchor: str, message: str
) -> None:
    plan = _write(tmp_path, text)
    expected = message.format(line=_line_of(text, anchor))

    for dry_run in (True, False):
        gh = FakeGh()
        with pytest.raises(ValueError, match=re.escape(expected)):
            backlog.import_backlog(plan, "o/r", gh, dry_run=dry_run)
        assert gh.calls == []


@pytest.mark.parametrize(
    ("text", "anchor", "message"),
    [
        pytest.param(
            MINI_PLAN.replace("- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-001", "- x"),
            "#### LH-001",
            "line {line}: LH-001 has no Epic line after its heading",
            id="no-epic-line",
        ),
        pytest.param(
            MINI_PLAN.replace("**Epic:** E1 · **Points:** 2", "**Epic:** E9 · **Points:** 2"),
            "#### LH-102",
            "line {line}: LH-102 uses E9, which is not in section 6.2",
            id="unknown-epic",
        ),
        pytest.param(
            MINI_PLAN.replace("**Points:** 5 · ", ""),
            "#### LH-109",
            "line {line}: LH-109 is missing a field matching",
            id="no-points",
        ),
        pytest.param(
            MINI_PLAN.replace("## Phase 0: Foundation (tag v0.0.1)\n", ""),
            "#### LH-001",
            "line {line}: LH-001 comes before any phase",
            id="story-before-any-phase",
        ),
    ],
)
def test_a_story_block_that_cannot_be_read_is_an_error(
    text: str, anchor: str, message: str
) -> None:
    expected = message.format(line=_line_of(text, anchor))

    with pytest.raises(ValueError, match=re.escape(expected)):
        backlog.parse_plan(text)


def test_a_story_id_used_twice_is_an_error() -> None:
    text = MINI_PLAN.replace("#### LH-002 ·", "#### LH-001 ·")
    first = _line_of(text, "#### LH-001 · Repo scaffold")
    second = _line_of(text, "#### LH-001 · Local stack")

    with pytest.raises(
        ValueError, match=f"line {second}: LH-001 is already defined on line {first}"
    ):
        backlog.parse_plan(text)


def test_a_mistyped_phase_heading_in_the_real_plan_stops_the_import(tmp_path: Path) -> None:
    text = PLAN.read_text(encoding="utf-8")
    heading = next(line for line in text.splitlines() if line.startswith("## Phase 4: "))
    plan = _write(tmp_path, text.replace(heading, heading.replace("(tag v", "(tag "), 1))
    gh = FakeGh()

    with pytest.raises(ValueError, match="malformed phase heading"):
        backlog.import_backlog(plan, "o/r", gh)

    assert gh.calls == []


def test_a_phase_heading_inside_a_code_block_is_not_a_phase() -> None:
    text = MINI_PLAN.replace("```bash\n", "```markdown\n## Phase 9: Not a phase (draft)\n")

    phases, stories = backlog.parse_plan(text)

    assert [phase.number for phase in phases] == [0, 1]
    assert len(stories) == 4


def test_fake_gh_rejects_an_unknown_label() -> None:
    gh = FakeGh()
    create = ["issue", "create", "--repo", "o/r", "--title", "t", "--body", "b"]

    with pytest.raises(backlog.GhError, match="could not add label: 'nope:1' not found"):
        gh.run([*create, "--label", "bug,nope:1"])
    assert gh.issues == []


def test_fake_gh_rejects_an_unknown_or_closed_milestone() -> None:
    gh = FakeGh()
    gh.milestones.append({"number": 1, "title": "Phase 0: A", "state": "closed"})
    create = ["issue", "create", "--repo", "o/r", "--title", "t", "--body", "b"]

    for name in ("Phase 0: A", "Phase 7: Nope"):
        with pytest.raises(backlog.GhError, match="could not add to milestone"):
            gh.run([*create, "--milestone", name])
    assert gh.issues == []


def test_fake_gh_needs_the_repo_on_every_call() -> None:
    gh = FakeGh()
    calls = [
        ["label", "list", "--limit", "5", "--json", "name"],
        ["label", "create", "x", "--force"],
        ["issue", "list", "--json", "number"],
        ["issue", "create", "--title", "t", "--body", "b"],
        ["api", "repos/elsewhere/milestones"],
    ]
    for call in calls:
        with pytest.raises(AssertionError):
            gh.run(call)


def test_fake_gh_honours_state_and_limit() -> None:
    gh = FakeGh()
    for title in ("one", "two", "three"):
        gh.run(["issue", "create", "--repo", "o/r", "--title", title, "--body", ""])
    gh.issues[0]["state"] = "closed"

    def numbers(*extra: str) -> list[int]:
        out = gh.run(["issue", "list", "--repo", "o/r", "--json", "number", *extra])
        return [issue["number"] for issue in json.loads(out)]

    assert numbers() == [3, 2]
    assert numbers("--state", "closed") == [1]
    assert numbers("--state", "all") == [3, 2, 1]
    assert numbers("--state", "all", "--limit", "2") == [3, 2]


def test_fake_gh_lists_milestones_like_the_rest_api() -> None:
    gh = FakeGh()
    for title in ("Phase 0: A", "Phase 1: B"):
        gh.run(["api", "-X", "POST", "repos/o/r/milestones", "-f", f"title={title}"])
    gh.milestones[0]["state"] = "closed"

    def titles(query: str) -> list[str]:
        return [m["title"] for m in json.loads(gh.run(["api", f"repos/o/r/milestones{query}"]))]

    assert titles("") == ["Phase 1: B"]
    assert titles("?state=all") == ["Phase 0: A", "Phase 1: B"]
    assert titles("?state=closed") == ["Phase 0: A"]
    assert titles("?state=all&per_page=1") == ["Phase 0: A"]
    with pytest.raises(backlog.GhError, match="422"):
        gh.run(["api", "-X", "POST", "repos/o/r/milestones", "-f", "title=Phase 1: B"])


def test_fake_gh_prints_nothing_for_a_repo_without_labels() -> None:
    gh = FakeGh(labels=())

    assert gh.run(["label", "list", "--repo", "o/r", "--limit", "500", "--json", "name"]) == ""


def test_import_creates_milestones_labels_and_issues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh()
    numbers = backlog.import_backlog(_write(tmp_path, MINI_PLAN), "o/r", gh)

    assert numbers == {"LH-001": 1, "LH-002": 2, "LH-102": 3, "LH-109": 4}
    assert [m["title"] for m in gh.milestones] == ["Phase 0: Foundation", "Phase 1: Agent core"]
    assert sorted(set(gh.labels) - set(DEFAULT_LABELS)) == [
        "could",
        "epic:E0",
        "epic:E1",
        "phase:0",
        "phase:1",
        "pts:2",
        "pts:3",
        "pts:5",
        "theme:T1",
        "theme:T5",
        "type:spike",
        "type:story",
    ]
    assert [(i["title"], i["milestone"], i["labels"]) for i in gh.issues] == [
        ("LH-001: Repo scaffold", "Phase 0: Foundation", STANDARD_LABELS),
        ("LH-002: Local stack", "Phase 0: Foundation", STANDARD_LABELS),
        (
            "LH-102: Spike: tool use",
            "Phase 1: Agent core",
            ["phase:1", "epic:E1", "pts:2", "theme:T1", "type:spike"],
        ),
        (
            "LH-109: Extras",
            "Phase 1: Agent core",
            ["phase:1", "epic:E1", "pts:5", "theme:T1", "type:story", "could"],
        ),
    ]
    assert not any(call[:2] == ["repo", "view"] for call in gh.calls)
    lines = capsys.readouterr().out.splitlines()
    assert "created milestone Phase 0: Foundation" in lines
    assert lines[-1] == "created LH-109 as #4"


def test_issue_body_is_the_story_block(tmp_path: Path) -> None:
    gh = FakeGh()
    backlog.import_backlog(_write(tmp_path, MINI_PLAN), "o/r", gh)

    assert gh.issue("LH-002")["body"] == (
        "#### LH-002 · Local stack\n"
        "- **Epic:** E0 · **Points:** 3 · **Branch:** `feat/lh-002-local-stack`"
        " · **Depends on:** LH-001\n"
        "- **Story:** As a developer, I want a local stack."
    )
    # A fenced block inside the story stays in the body, heading and all.
    first = gh.issue("LH-001")["body"]
    assert first.startswith("#### LH-001 · Repo scaffold\n- **Epic:** E0")
    assert first.endswith("```bash\n#### LH-050 · A heading inside a fence is not a story\n```")
    # The text under a later heading is not part of the story above it.
    assert gh.issue("LH-109")["body"].endswith("- **Story:** As a developer, I want extras.")


def test_import_into_a_repo_without_labels(tmp_path: Path) -> None:
    gh = FakeGh(labels=())

    numbers = backlog.import_backlog(_write(tmp_path, MINI_PLAN), "o/r", gh)

    assert len(numbers) == len(gh.issues) == 4
    assert "phase:0" in gh.labels
    assert "bug" not in gh.labels  # only the labels of the plan were added


def test_second_import_into_a_repo_that_started_without_labels(tmp_path: Path) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh(labels=())
    first = backlog.import_backlog(plan, "o/r", gh)
    gh.calls.clear()

    assert backlog.import_backlog(plan, "o/r", gh) == first
    assert gh.writes() == []


class CannedGh:
    """Answers every list call with a fixed reply, to see how the importer reads it."""

    def __init__(
        self, label_reply: str = "[]", issue_reply: str = "[]", api_reply: str = "[]"
    ) -> None:
        self.replies = {"label list": label_reply, "issue list": issue_reply, "api": api_reply}
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> str:
        self.calls.append(args)
        return self.replies[args[0] if args[0] == "api" else " ".join(args[:2])]


@pytest.mark.parametrize(
    ("reply", "command"),
    [
        pytest.param({"label_reply": "no labels yet"}, "gh label list --repo o/r", id="labels"),
        pytest.param({"issue_reply": "<html>"}, "gh issue list --repo o/r", id="issues"),
        pytest.param({"api_reply": "{"}, "gh api repos/o/r/milestones", id="milestones"),
        pytest.param(
            {"label_reply": '{"name": "bug"}'}, "gh label list --repo o/r", id="not-a-list"
        ),
    ],
)
def test_a_gh_reply_that_is_not_a_json_list_names_the_command(
    tmp_path: Path, reply: dict[str, str], command: str
) -> None:
    gh = CannedGh(**reply)

    with pytest.raises(backlog.GhError, match=re.escape(command)):
        backlog.import_backlog(_write(tmp_path, MINI_PLAN), "o/r", gh)

    assert gh.calls  # it did ask, and it stopped before writing anything


def test_import_places_every_story_of_the_real_plan() -> None:
    plan_text = PLAN.read_text(encoding="utf-8")
    heading_phases, heading_stories = _plan_headings(plan_text)
    gh = FakeGh()

    numbers = backlog.import_backlog(PLAN, "o/r", gh)

    assert len(numbers) == len(gh.issues) == len(heading_stories)
    assert [m["title"] for m in gh.milestones] == [
        f"Phase {number}: {name}" for number, name in heading_phases
    ]
    for story_id, title in heading_stories:
        issue = gh.issue(story_id)
        phase_digit = story_id[3]
        assert issue["title"] == f"{story_id}: {title.removesuffix(' (Could)')}"
        assert issue["milestone"].startswith(f"Phase {phase_digit}: ")
        assert issue["labels"][0] == f"phase:{phase_digit}"
        assert numbers[story_id] == issue["number"]
    assert "type:spike" in gh.issue("LH-102")["labels"]
    assert "could" in gh.issue("LH-908")["labels"]
    assert "could" not in gh.issue("LH-001")["labels"]


def test_import_uses_the_default_repo_once(tmp_path: Path) -> None:
    gh = FakeGh(repo="acme/longhaul")

    backlog.import_backlog(_write(tmp_path, MINI_PLAN), None, gh)

    repo_views = [call for call in gh.calls if call[:2] == ["repo", "view"]]
    assert repo_views == [gh.calls[0]]
    assert len(gh.issues) == 4


def test_existing_milestones_and_labels_are_left_alone(tmp_path: Path) -> None:
    gh = FakeGh(labels=[*DEFAULT_LABELS, "phase:0"])
    gh.milestones.append({"number": 1, "title": "Phase 0: Foundation", "state": "open"})

    backlog.import_backlog(_write(tmp_path, MINI_PLAN), "o/r", gh)

    created_labels = [call[2] for call in gh.calls if call[:2] == ["label", "create"]]
    created_milestones = [call[-1] for call in gh.calls if call[0] == "api" and "-X" in call]
    assert "phase:0" not in created_labels
    assert "phase:1" in created_labels
    assert created_milestones == ["title=Phase 1: Agent core"]


def test_idempotent_plan() -> None:
    phases, stories = backlog.parse_plan(PLAN.read_text(encoding="utf-8"))
    gh = FakeGh()
    first = backlog.import_backlog(PLAN, "o/r", gh, dry_run=False, update=False)
    assert len(first) == len(stories)
    assert len(gh.milestones) == len(phases)
    assert len(gh.issues) == len(stories)

    gh.calls.clear()
    second = backlog.import_backlog(PLAN, "o/r", gh, dry_run=False, update=False)

    assert gh.calls  # it looked at GitHub before deciding that nothing was missing
    assert gh.writes() == []
    assert second == first
    assert len(gh.issues) == len(stories)


def test_closed_issues_still_count_as_imported(tmp_path: Path) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh()
    first = backlog.import_backlog(plan, "o/r", gh)
    gh.issues[1]["state"] = "closed"
    gh.calls.clear()

    second = backlog.import_backlog(plan, "o/r", gh)

    assert second == first
    assert gh.writes() == []
    assert len(gh.issues) == 4


@pytest.mark.parametrize("update", [False, True])
def test_a_closed_milestone_is_left_closed_when_nothing_needs_it(
    tmp_path: Path, update: bool
) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh()
    backlog.import_backlog(plan, "o/r", gh)
    gh.milestones[0]["state"] = "closed"  # the owner closes Phase 0 after its gate
    gh.calls.clear()

    backlog.import_backlog(plan, "o/r", gh, update=update)

    assert gh.writes() == []
    assert [m["state"] for m in gh.milestones] == ["closed", "open"]


def _mini_plan_with_fix_story(phase: int) -> str:
    """MINI_PLAN plus a gate fix story, LH-091 in phase 0 or LH-191 in phase 1."""
    if phase == 0:
        return _add_fix_story(MINI_PLAN, "LH-091")
    return MINI_PLAN + "\n" + FIX_STORY.format(id="LH-191", slug="lh-191").replace("E0", "E1")


@pytest.mark.parametrize("phase", [0, 1])
def test_a_closed_milestone_is_reopened_for_a_new_story(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], phase: int
) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh()
    backlog.import_backlog(plan, "o/r", gh)
    name = gh.milestones[phase]["title"]
    gh.milestones[phase]["state"] = "closed"
    gh.calls.clear()
    capsys.readouterr()
    # A failed gate adds a fix story to a phase whose milestone the owner already closed.
    plan.write_text(_mini_plan_with_fix_story(phase), encoding="utf-8")

    numbers = backlog.import_backlog(plan, "o/r", gh)

    writes = gh.writes()
    patch = ["api", "-X", "PATCH", f"repos/o/r/milestones/{phase + 1}", "-f", "state=open"]
    assert writes[0] == patch
    assert [write[:2] for write in writes[1:]] == [["label", "create"], ["issue", "create"]]
    assert numbers[f"LH-{phase}91"] == 5
    assert gh.issue(f"LH-{phase}91")["milestone"] == name
    assert [m["state"] for m in gh.milestones] == ["open", "open"]
    assert f"reopened milestone {name}" in capsys.readouterr().out.splitlines()


def test_a_closed_milestone_stays_closed_when_the_new_story_is_elsewhere(tmp_path: Path) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh()
    backlog.import_backlog(plan, "o/r", gh)
    gh.milestones[0]["state"] = "closed"
    gh.calls.clear()
    plan.write_text(_mini_plan_with_fix_story(1), encoding="utf-8")

    backlog.import_backlog(plan, "o/r", gh)

    # Only type:fix is a new label, and Phase 0 is not touched at all.
    assert [write[:2] for write in gh.writes()] == [["label", "create"], ["issue", "create"]]
    assert gh.issue("LH-191")["milestone"] == "Phase 1: Agent core"
    assert [m["state"] for m in gh.milestones] == ["closed", "open"]


def test_a_milestone_that_cannot_be_reopened_stops_the_run_before_any_issue(
    tmp_path: Path,
) -> None:
    class NoReopen(FakeGh):
        def _milestone_patch(self, number: int, fields: dict[str, str]) -> str:
            raise backlog.GhError("gh: Must have admin rights to Repository. (HTTP 403)")

    plan = _write(tmp_path, MINI_PLAN)
    gh = NoReopen()
    backlog.import_backlog(plan, "o/r", gh)
    gh.milestones[0]["state"] = "closed"
    gh.calls.clear()
    plan.write_text(_mini_plan_with_fix_story(0), encoding="utf-8")

    with pytest.raises(backlog.GhError, match="403"):
        backlog.import_backlog(plan, "o/r", gh)

    assert gh.writes() == [["api", "-X", "PATCH", "repos/o/r/milestones/1", "-f", "state=open"]]
    assert len(gh.issues) == 4


def test_update_leaves_unchanged_issues_alone(tmp_path: Path) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh()
    backlog.import_backlog(plan, "o/r", gh)
    gh.calls.clear()

    numbers = backlog.import_backlog(plan, "o/r", gh, update=True)

    assert gh.writes() == []
    assert numbers == {"LH-001": 1, "LH-002": 2, "LH-102": 3, "LH-109": 4}


def test_update_ignores_line_endings_and_edge_whitespace(tmp_path: Path) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh()
    backlog.import_backlog(plan, "o/r", gh)
    # GitHub hands bodies back with CRLF line endings.
    for issue in gh.issues:
        issue["body"] = issue["body"].replace("\n", "\r\n") + "\r\n"
    gh.issues[0]["body"] = "  " + gh.issues[0]["body"]
    gh.calls.clear()

    backlog.import_backlog(plan, "o/r", gh, update=True)

    assert gh.writes() == []


def test_update_rewrites_only_the_issue_whose_body_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh()
    backlog.import_backlog(plan, "o/r", gh)
    gh.calls.clear()
    capsys.readouterr()
    plan.write_text(MINI_PLAN.replace("I want a local stack", "I want a faster stack"), "utf-8")

    backlog.import_backlog(plan, "o/r", gh, update=True)

    edits = gh.writes()
    assert len(edits) == 1
    assert edits[0][:5] == ["issue", "edit", "2", "--repo", "o/r"]
    assert edits[0][5] == "--body"
    assert "I want a faster stack" in gh.issue("LH-002")["body"]
    assert "I want a scaffold" in gh.issue("LH-001")["body"]
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "skipped LH-001 (#1)"
    assert lines[1].startswith("updated LH-002 (#2)")


def _imported(tmp_path: Path, text: str = MINI_PLAN) -> tuple[FakeGh, Path]:
    """Import a plan into a fresh FakeGh and forget the calls that took."""
    plan = _write(tmp_path, text)
    gh = FakeGh()
    backlog.import_backlog(plan, "o/r", gh)
    gh.calls.clear()
    return gh, plan


def test_update_syncs_the_labels_when_the_points_change(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    plan.write_text(
        MINI_PLAN.replace(
            "**Points:** 3 · **Branch:** `feat/lh-002", "**Points:** 8 · **Branch:** `feat/lh-002"
        ),
        "utf-8",
    )

    backlog.import_backlog(plan, "o/r", gh, update=True)

    issue = gh.issue("LH-002")
    edit = ["issue", "edit", "2", "--repo", "o/r", "--body", issue["body"]]
    edit += ["--add-label", "pts:8", "--remove-label", "pts:3"]
    assert gh.writes() == [["label", "create", "pts:8", "--repo", "o/r", "--force"], edit]
    assert "**Points:** 8" in issue["body"]
    assert sorted(issue["labels"]) == ["epic:E0", "phase:0", "pts:8", "theme:T5", "type:story"]


def test_update_syncs_the_title(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    plan.write_text(
        MINI_PLAN.replace("LH-002 · Local stack", "LH-002 · Local compose stack"), "utf-8"
    )

    backlog.import_backlog(plan, "o/r", gh, update=True)

    assert [write[:5] for write in gh.writes()] == [["issue", "edit", "2", "--repo", "o/r"]]
    assert gh.issue("LH-002")["title"] == "LH-002: Local compose stack"
    assert gh.writes()[0][5:7] == ["--title", "LH-002: Local compose stack"]


def test_update_moves_an_issue_to_the_milestone_of_its_phase(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    gh.issue("LH-001")["milestone"] = "Phase 1: Agent core"
    gh.issue("LH-002")["milestone"] = None

    backlog.import_backlog(plan, "o/r", gh, update=True)

    assert gh.writes() == [
        ["issue", "edit", "1", "--repo", "o/r", "--milestone", "Phase 0: Foundation"],
        ["issue", "edit", "2", "--repo", "o/r", "--milestone", "Phase 0: Foundation"],
    ]
    assert gh.issue("LH-001")["milestone"] == "Phase 0: Foundation"
    assert gh.issue("LH-002")["milestone"] == "Phase 0: Foundation"


def test_update_adds_a_missing_label_and_keeps_labels_a_person_added(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    without_theme = ["phase:0", "epic:E0", "pts:3", "type:story"]
    gh.issue("LH-001")["labels"] = [*without_theme, "bug", "help wanted"]

    backlog.import_backlog(plan, "o/r", gh, update=True)

    assert gh.writes() == [["issue", "edit", "1", "--repo", "o/r", "--add-label", "theme:T5"]]
    assert sorted(gh.issue("LH-001")["labels"]) == sorted([*STANDARD_LABELS, "bug", "help wanted"])


def test_update_fixes_several_labels_in_one_edit(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    gh.issue("LH-001")["labels"] = ["phase:1", "epic:E1", "pts:3", "theme:T5", "type:story", "bug"]
    gh.issue("LH-002")["labels"] = ["bug"]

    backlog.import_backlog(plan, "o/r", gh, update=True)

    first = ["issue", "edit", "1", "--repo", "o/r"]
    first += ["--add-label", "phase:0,epic:E0", "--remove-label", "epic:E1,phase:1"]
    second = ["issue", "edit", "2", "--repo", "o/r", "--add-label", ",".join(STANDARD_LABELS)]
    assert gh.writes() == [first, second]
    assert sorted(gh.issue("LH-001")["labels"]) == sorted([*STANDARD_LABELS, "bug"])
    assert sorted(gh.issue("LH-002")["labels"]) == sorted([*STANDARD_LABELS, "bug"])


def test_update_removes_a_derived_label_the_plan_no_longer_has(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    plan.write_text(MINI_PLAN.replace("Extras (Could)", "Extras"), "utf-8")

    backlog.import_backlog(plan, "o/r", gh, update=True)

    (edit,) = gh.writes()
    assert edit[:5] == ["issue", "edit", "4", "--repo", "o/r"]
    assert edit[-2:] == ["--remove-label", "could"]
    assert "could" not in gh.issue("LH-109")["labels"]


# One stale label per prefix the importer owns. Written out here, not read from the
# importer, so a prefix that drops out of the importer's list fails this test.
@pytest.mark.parametrize("stale", ["phase:9", "epic:E9", "pts:99", "theme:T9", "type:fix"])
def test_update_removes_a_stale_label_of_every_derived_kind(tmp_path: Path, stale: str) -> None:
    gh, plan = _imported(tmp_path)
    gh.issue("LH-001")["labels"] = [*STANDARD_LABELS, stale, "bug"]

    backlog.import_backlog(plan, "o/r", gh, update=True)

    assert gh.writes() == [["issue", "edit", "1", "--repo", "o/r", "--remove-label", stale]]
    assert sorted(gh.issue("LH-001")["labels"]) == sorted([*STANDARD_LABELS, "bug"])


def test_update_converges(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    text = MINI_PLAN.replace("LH-002 · Local stack", "LH-002 · Local compose stack")
    text = text.replace(
        "**Points:** 3 · **Branch:** `feat/lh-002", "**Points:** 8 · **Branch:** `feat/lh-002"
    )
    plan.write_text(text, "utf-8")
    gh.issue("LH-001")["milestone"] = None
    backlog.import_backlog(plan, "o/r", gh, update=True)
    gh.calls.clear()

    backlog.import_backlog(plan, "o/r", gh, update=True)

    assert gh.writes() == []


def test_update_says_which_parts_of_an_issue_it_changed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh, plan = _imported(tmp_path)
    text = MINI_PLAN.replace("LH-002 · Local stack", "LH-002 · Local compose stack")
    text = text.replace(
        "**Points:** 3 · **Branch:** `feat/lh-002", "**Points:** 8 · **Branch:** `feat/lh-002"
    )
    plan.write_text(text, "utf-8")
    gh.issue("LH-001")["milestone"] = None
    capsys.readouterr()

    backlog.import_backlog(plan, "o/r", gh, update=True)

    # This is the only trace of what --update did to the public issues.
    assert capsys.readouterr().out.splitlines() == [
        "created label pts:8",
        "updated LH-001 (#1): milestone",
        "updated LH-002 (#2): title, body, labels",
        "skipped LH-102 (#3)",
        "skipped LH-109 (#4)",
    ]


def test_update_right_after_an_import_changes_nothing_in_the_real_plan() -> None:
    gh = FakeGh()
    backlog.import_backlog(PLAN, "o/r", gh)
    gh.calls.clear()

    backlog.import_backlog(PLAN, "o/r", gh, update=True)

    assert gh.writes() == []


def test_update_ignores_issues_that_are_not_stories(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    gh.run(["issue", "create", "--repo", "o/r", "--title", "Crash on start", "--body", "help"])
    gh.calls.clear()

    backlog.import_backlog(plan, "o/r", gh, update=True)

    assert gh.writes() == []
    assert gh.issues[-1]["labels"] == []


def test_update_reopens_a_closed_milestone_it_moves_an_issue_into(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    gh.milestones[0]["state"] = "closed"
    gh.issue("LH-001")["milestone"] = None

    backlog.import_backlog(plan, "o/r", gh, update=True)

    assert gh.writes() == [
        ["api", "-X", "PATCH", "repos/o/r/milestones/1", "-f", "state=open"],
        ["issue", "edit", "1", "--repo", "o/r", "--milestone", "Phase 0: Foundation"],
    ]


def test_without_update_a_drifted_issue_does_not_reopen_its_closed_milestone(
    tmp_path: Path,
) -> None:
    gh, plan = _imported(tmp_path)
    gh.milestones[0]["state"] = "closed"
    gh.issue("LH-001")["milestone"] = None

    backlog.import_backlog(plan, "o/r", gh, update=False)

    # Nothing is moved without --update, so there is no reason to touch the milestone.
    assert gh.writes() == []
    assert gh.milestones[0]["state"] == "closed"


def test_drifted_labels_title_and_milestone_are_left_alone_without_update(tmp_path: Path) -> None:
    gh, plan = _imported(tmp_path)
    issue = gh.issue("LH-001")
    issue.update(title="LH-001: Renamed by hand", labels=["bug"], milestone=None)

    backlog.import_backlog(plan, "o/r", gh, update=False)

    assert gh.writes() == []
    assert issue["title"] == "LH-001: Renamed by hand"


def test_a_changed_body_is_left_alone_without_update(tmp_path: Path) -> None:
    plan = _write(tmp_path, MINI_PLAN)
    gh = FakeGh()
    backlog.import_backlog(plan, "o/r", gh)
    gh.calls.clear()
    plan.write_text(MINI_PLAN.replace("I want a local stack", "I want a faster stack"), "utf-8")

    backlog.import_backlog(plan, "o/r", gh, update=False)

    assert gh.writes() == []
    assert "I want a local stack" in gh.issue("LH-002")["body"]


def test_dry_run_makes_no_calls(capsys: pytest.CaptureFixture[str]) -> None:
    heading_phases, heading_stories = _plan_headings(PLAN.read_text(encoding="utf-8"))
    gh = FakeGh()
    numbers = backlog.import_backlog(PLAN, None, gh, dry_run=True, update=False)

    assert gh.calls == []
    assert numbers == {}
    lines = capsys.readouterr().out.splitlines()
    assert lines[-1] == f"{len(heading_stories)} stories, {len(heading_phases)} milestones"
    assert [line for line in lines if line.startswith("milestone  ")] == [
        f"milestone  Phase {number}: {name}" for number, name in heading_phases
    ]
    assert len([line for line in lines if line.startswith("story      ")]) == len(heading_stories)


def test_dry_run_lists_every_milestone_and_story(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = FakeGh()

    backlog.import_backlog(_write(tmp_path, MINI_PLAN), None, gh, dry_run=True)

    assert gh.calls == []
    assert capsys.readouterr().out.splitlines() == [
        "milestone  Phase 0: Foundation",
        "milestone  Phase 1: Agent core",
        "story      LH-001: Repo scaffold  [phase:0, epic:E0, pts:3, theme:T5, type:story]",
        "story      LH-002: Local stack  [phase:0, epic:E0, pts:3, theme:T5, type:story]",
        "story      LH-102: Spike: tool use  [phase:1, epic:E1, pts:2, theme:T1, type:spike]",
        "story      LH-109: Extras  [phase:1, epic:E1, pts:5, theme:T1, type:story, could]",
        "4 stories, 2 milestones",
    ]


@dataclass
class Workspace:
    gh: FakeGh
    plan: Path
    progress: Path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """A plan and a progress file in a temp dir, with `main()` wired to a FakeGh."""
    gh = FakeGh()
    progress = _write(tmp_path, MINI_PROGRESS, "progress.md")
    monkeypatch.setattr(backlog, "GhRunner", lambda: gh)
    monkeypatch.setattr(backlog, "DEFAULT_PROGRESS", progress)
    return Workspace(gh, _write(tmp_path, MINI_PLAN), progress)


def test_main_imports_and_writes_issue_numbers_into_progress(workspace: Workspace) -> None:
    assert backlog.main([str(workspace.plan), "--repo", "o/r"]) == 0

    assert not any(call[:2] == ["repo", "view"] for call in workspace.gh.calls)
    text = workspace.progress.read_text(encoding="utf-8")
    assert "| ID | Title | Pts | Status | PR | Merged | Issue |" in text
    assert "| LH-001 | Repo scaffold | 3 | Todo | | | #1 |" in text
    assert "| LH-002 | Local stack | 3 | Todo | | | #2 |" in text
    assert "| LH-102 | Spike: tool use | 2 | Todo | | | #3 |" in text
    assert "| LH-109 | Extras | 5 | Todo | | | #4 |" in text


def test_main_run_twice_keeps_the_same_numbers(workspace: Workspace) -> None:
    backlog.main([str(workspace.plan), "--repo", "o/r"])
    first = workspace.progress.read_text(encoding="utf-8")

    assert backlog.main([str(workspace.plan), "--repo", "o/r"]) == 0

    assert workspace.progress.read_text(encoding="utf-8") == first
    assert len(workspace.gh.issues) == 4


def test_main_without_repo_asks_gh_for_it(workspace: Workspace) -> None:
    assert backlog.main([str(workspace.plan)]) == 0

    assert workspace.gh.calls[0][:2] == ["repo", "view"]


def test_main_dry_run_calls_nothing_and_keeps_progress(
    workspace: Workspace, capsys: pytest.CaptureFixture[str]
) -> None:
    assert backlog.main([str(workspace.plan), "--dry-run"]) == 0

    assert workspace.gh.calls == []
    assert workspace.progress.read_text(encoding="utf-8") == MINI_PROGRESS
    assert capsys.readouterr().out.splitlines()[-1] == "4 stories, 2 milestones"


def test_main_update_flag_rewrites_changed_bodies(workspace: Workspace) -> None:
    backlog.main([str(workspace.plan), "--repo", "o/r"])
    workspace.plan.write_text(MINI_PLAN.replace("a scaffold", "a better scaffold"), "utf-8")

    assert backlog.main([str(workspace.plan), "--repo", "o/r"]) == 0
    assert "a better scaffold" not in workspace.gh.issue("LH-001")["body"]
    assert backlog.main([str(workspace.plan), "--repo", "o/r", "--update"]) == 0

    assert "a better scaffold" in workspace.gh.issue("LH-001")["body"]


def test_main_reports_a_gh_failure_and_keeps_progress(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class LoggedOut:
        def run(self, args: list[str]) -> str:
            raise backlog.GhError("gh: not logged in")

    monkeypatch.setattr(backlog, "GhRunner", LoggedOut)

    assert backlog.main([str(workspace.plan), "--repo", "o/r"]) == 1

    assert capsys.readouterr().err == "error: gh: not logged in\n"
    assert workspace.progress.read_text(encoding="utf-8") == MINI_PROGRESS


def test_main_reports_a_malformed_plan_before_calling_gh(
    workspace: Workspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _write(tmp_path, "## Phase 0: Foundation (tag v0.0.1)\n\n#### LH-12 · Bad\n", "bad.md")

    assert backlog.main([str(bad), "--repo", "o/r"]) == 1

    assert capsys.readouterr().err.startswith("error: line 3: malformed story heading")
    assert workspace.gh.calls == []
    assert workspace.progress.read_text(encoding="utf-8") == MINI_PROGRESS


def test_main_reports_a_missing_plan_file(
    workspace: Workspace, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert backlog.main([str(tmp_path / "missing.md")]) == 1

    assert capsys.readouterr().err.startswith("error: ")
    assert workspace.gh.calls == []


def _stub_gh(tmp_path: Path, script: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


def test_gh_runner_returns_stdout_and_wraps_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = 'if [ "$1" = fail ]; then echo "no luck" >&2; exit 3; fi\necho "$@"\n'
    monkeypatch.setenv("PATH", str(_stub_gh(tmp_path, script)))
    runner = backlog.GhRunner()

    assert runner.run(["issue", "list", "--repo", "o/r"]) == "issue list --repo o/r\n"
    with pytest.raises(backlog.GhError, match="gh fail now failed: no luck"):
        runner.run(["fail", "now"])


def test_main_reports_a_missing_gh_binary(
    workspace: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(backlog, "GhRunner", REAL_GH_RUNNER)
    monkeypatch.setenv("PATH", str(empty))

    assert backlog.main([str(workspace.plan), "--repo", "o/r"]) == 1

    assert "gh" in capsys.readouterr().err
    assert workspace.progress.read_text(encoding="utf-8") == MINI_PROGRESS


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
