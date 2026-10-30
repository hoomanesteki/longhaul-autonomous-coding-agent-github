"""Import the build plan's stories into GitHub milestones, labels and issues.

Stdlib only. Every GitHub call goes through `gh`, so the login that `gh` holds
is the only credential involved. Re-running is safe: milestones, labels and
issues that already exist are matched by name or by the `LH-xxx:` title prefix
and left alone.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROGRESS = ROOT / "docs" / "progress.md"

PHASE_RE = re.compile(r"^## Phase (\d): (.+) \(tag (v[\d.]+)\)$")
PHASE_START_RE = re.compile(r"^## Phase \d")
STORY_PREFIX = "#### LH"
STORY_RE = re.compile(r"^#### (LH-\d{3}) · (.+)$")
HEADING_RE = re.compile(r"^#{1,6} ")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
EPIC_ROW_RE = re.compile(r"^\| (E\d+) \| (.+?) \| (T\d+) \|")
EPIC_FIELD_RE = re.compile(r"\*\*Epic:\*\* (E\d+)")
POINTS_FIELD_RE = re.compile(r"\*\*Points:\*\* (\d+)")
BRANCH_FIELD_RE = re.compile(r"\*\*Branch:\*\* (.+?) · \*\*Depends on:\*\*")
DEPENDS_FIELD_RE = re.compile(r"\*\*Depends on:\*\* (.+)$")
COULD_SUFFIX = " (Could)"
TITLE_PREFIX_RE = re.compile(r"^(LH-\d{3}): ")
PROGRESS_ROW_RE = re.compile(r"^\| (LH-\d{3}) \|")


@dataclass(frozen=True)
class Phase:
    number: int
    name: str
    tag: str


@dataclass(frozen=True)
class Story:
    id: str
    title: str
    phase: int
    epic: str
    theme: str
    points: int
    kind: str  # story | spike | fix
    could: bool
    branch: str
    depends_on: str
    body: str


@dataclass
class _Block:
    line: int
    heading: str
    id: str
    title: str
    phase: int
    lines: list[str]


class GhError(RuntimeError):
    pass


class Runner(Protocol):
    def run(self, args: list[str]) -> str: ...


class GhRunner:
    """Runs `gh` with the given arguments and returns stdout."""

    def run(self, args: list[str]) -> str:
        proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise GhError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout


def _walk(text: str) -> Iterator[tuple[int, str, bool]]:
    """Yield (line number, line, inside_code). Fence lines count as code."""
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if fence is not None:
            yield number, line, True
            if _closes_fence(line, fence):
                fence = None
            continue
        opening = FENCE_RE.match(line)
        if opening:
            fence = opening.group(1)
            yield number, line, True
            continue
        yield number, line, False


def _closes_fence(line: str, fence: str) -> bool:
    # A block opened with ```` only closes on a line of at least four backticks,
    # so a ``` line inside it is ordinary code.
    stripped = line.strip()
    return len(stripped) >= len(fence) and set(stripped) == {fence[0]}


def _field(pattern: re.Pattern[str], text: str, block: _Block) -> str:
    match = pattern.search(text)
    if match is None:
        raise ValueError(
            f"line {block.line}: {block.id} is missing a field matching {pattern.pattern}"
        )
    return match.group(1)


def _build_story(block: _Block, themes: dict[str, str]) -> Story:
    field_line = next((line for line in block.lines if line.strip()), "")
    if not field_line.startswith("- **Epic:**"):
        raise ValueError(f"line {block.line}: {block.id} has no Epic line after its heading")
    epic = _field(EPIC_FIELD_RE, field_line, block)
    if epic not in themes:
        raise ValueError(f"line {block.line}: {block.id} uses {epic}, which is not in section 6.2")
    branch = _field(BRANCH_FIELD_RE, field_line, block).replace("`", "")
    title = block.title
    could = title.endswith(COULD_SUFFIX)
    if could:
        title = title[: -len(COULD_SUFFIX)]
    if title.startswith("Spike"):
        kind = "spike"
    elif branch.startswith("fix/"):
        kind = "fix"
    else:
        kind = "story"
    body = "\n".join([block.heading, *block.lines]).rstrip()
    return Story(
        id=block.id,
        title=title,
        phase=block.phase,
        epic=epic,
        theme=themes[epic],
        points=int(_field(POINTS_FIELD_RE, field_line, block)),
        kind=kind,
        could=could,
        branch=branch,
        depends_on=_field(DEPENDS_FIELD_RE, field_line, block),
        body=body,
    )


def _start_phase(phases: list[Phase], line: str, number: int) -> None:
    match = PHASE_RE.match(line)
    if match is None:
        raise ValueError(f"line {number}: malformed phase heading: {line!r}")
    found = int(match.group(1))
    if found != len(phases):
        raise ValueError(
            f"line {number}: found Phase {found} where Phase {len(phases)} should come next,"
            " because phases must run 0..N in order"
        )
    phases.append(Phase(found, match.group(2), match.group(3)))


def _start_story(phases: list[Phase], blocks: list[_Block], line: str, number: int) -> _Block:
    match = STORY_RE.match(line)
    if match is None:
        raise ValueError(f"line {number}: malformed story heading: {line!r}")
    story_id = match.group(1)
    if not phases:
        raise ValueError(f"line {number}: {story_id} comes before any phase")
    # The first digit of a story id is its phase, so a mismatch means a heading above is wrong.
    phase = phases[-1].number
    id_phase = int(story_id[3])
    if id_phase != phase:
        raise ValueError(
            f"line {number}: {story_id} sits under Phase {phase}, but its id says phase {id_phase}"
        )
    earlier = next((block for block in blocks if block.id == story_id), None)
    if earlier is not None:
        raise ValueError(f"line {number}: {story_id} is already defined on line {earlier.line}")
    block = _Block(number, line, story_id, match.group(2), phase, [])
    blocks.append(block)
    return block


def parse_plan(text: str) -> tuple[list[Phase], list[Story]]:
    """Return the phases and stories of the build plan.

    Headings inside fenced code blocks are ignored. A `## Phase` or `#### LH` heading
    that does not match its pattern is an error, because it would otherwise be dropped
    without a trace and its stories would land in the wrong milestone. Phases must run
    0..N in order and a story id must start with the number of the phase it sits in.
    """
    phases: list[Phase] = []
    blocks: list[_Block] = []
    themes: dict[str, str] = {}
    current: _Block | None = None

    for number, line, in_code in _walk(text):
        if in_code:
            if current is not None:
                current.lines.append(line)
            continue
        if PHASE_START_RE.match(line):
            current = None
            _start_phase(phases, line, number)
            continue
        if line.startswith(STORY_PREFIX):
            current = _start_story(phases, blocks, line, number)
            continue
        if HEADING_RE.match(line):
            current = None
            continue
        row = EPIC_ROW_RE.match(line)
        if row:
            themes[row.group(1)] = row.group(3)
        if current is not None:
            current.lines.append(line)

    return phases, [_build_story(block, themes) for block in blocks]


def phase_milestone(phase: Phase) -> str:
    return f"Phase {phase.number}: {phase.name}"


def story_labels(story: Story) -> list[str]:
    labels = [
        f"phase:{story.phase}",
        f"epic:{story.epic}",
        f"pts:{story.points}",
        f"theme:{story.theme}",
        f"type:{story.kind}",
    ]
    if story.could:
        labels.append("could")
    return labels


@dataclass
class _Remote:
    milestones: set[str]
    labels: set[str]
    issues: dict[str, tuple[int, str]]  # LH id -> (issue number, body)


def _load_remote(runner: Runner, repo: str) -> _Remote:
    milestones = json.loads(runner.run(["api", f"repos/{repo}/milestones?state=all&per_page=100"]))
    labels = json.loads(
        runner.run(["label", "list", "--repo", repo, "--limit", "500", "--json", "name"])
    )
    issues = json.loads(
        runner.run(
            [
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "all",
                "--limit",
                "500",
                "--json",
                "number,title,body",
            ]
        )
    )
    issue_map: dict[str, tuple[int, str]] = {}
    for issue in issues:
        prefix = TITLE_PREFIX_RE.match(issue["title"])
        if prefix:
            issue_map[prefix.group(1)] = (int(issue["number"]), issue.get("body") or "")
    return _Remote(
        milestones={m["title"] for m in milestones},
        labels={label["name"] for label in labels},
        issues=issue_map,
    )


def _number_from_url(output: str) -> int:
    url = output.strip().splitlines()[-1]
    return int(url.rstrip("/").rsplit("/", 1)[-1])


def _default_repo(runner: Runner) -> str:
    return runner.run(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).strip()


def import_backlog(
    plan_path: Path,
    repo: str | None,
    runner: Runner | None = None,
    dry_run: bool = False,
    update: bool = False,
) -> dict[str, int]:
    """Create milestones, labels and issues for every story in the plan.

    Returns the issue number for each story id. Dry runs make no runner calls
    and return an empty mapping.
    """
    phases, stories = parse_plan(plan_path.read_text(encoding="utf-8"))
    milestones = {phase.number: phase_milestone(phase) for phase in phases}

    if dry_run:
        for name in milestones.values():
            print(f"milestone  {name}")
        for story in stories:
            print(f"story      {story.id}: {story.title}  [{', '.join(story_labels(story))}]")
        print(f"{len(stories)} stories, {len(milestones)} milestones")
        return {}

    gh = runner if runner is not None else GhRunner()
    target = repo if repo is not None else _default_repo(gh)
    remote = _load_remote(gh, target)

    for name in milestones.values():
        if name in remote.milestones:
            continue
        gh.run(["api", "-X", "POST", f"repos/{target}/milestones", "-f", f"title={name}"])
        remote.milestones.add(name)
        print(f"created milestone {name}")

    wanted_labels = sorted({label for story in stories for label in story_labels(story)})
    for label in wanted_labels:
        if label in remote.labels:
            continue
        gh.run(["label", "create", label, "--repo", target, "--force"])
        remote.labels.add(label)
        print(f"created label {label}")

    numbers: dict[str, int] = {}
    for story in stories:
        existing = remote.issues.get(story.id)
        if existing is None:
            output = gh.run(
                [
                    "issue",
                    "create",
                    "--repo",
                    target,
                    "--title",
                    f"{story.id}: {story.title}",
                    "--body",
                    story.body,
                    "--label",
                    ",".join(story_labels(story)),
                    "--milestone",
                    milestones[story.phase],
                ]
            )
            numbers[story.id] = _number_from_url(output)
            print(f"created {story.id} as #{numbers[story.id]}")
            continue
        number, body = existing
        numbers[story.id] = number
        if update and _normalize(body) != _normalize(story.body):
            gh.run(["issue", "edit", str(number), "--repo", target, "--body", story.body])
            print(f"updated {story.id} (#{number})")
        else:
            print(f"skipped {story.id} (#{number})")
    return numbers


def _normalize(body: str) -> str:
    # GitHub stores issue bodies with CRLF line endings; compare content, not newlines.
    return body.replace("\r\n", "\n").strip()


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip()[1:-1].split("|")]


def _format_row(cells: list[str]) -> str:
    return "|" + "".join(f" {cell} |" if cell else " |" for cell in cells)


def add_issue_numbers(text: str, numbers: dict[str, int]) -> str:
    """Add an Issue column to each progress table and fill it from `numbers`.

    Tables that already have the column keep it. Rows for stories with no
    known issue keep whatever the cell held before.
    """
    out: list[str] = []
    header_cols: int | None = None
    expect_separator = False
    for line in text.splitlines():
        if line.startswith("| ID |"):
            cells = _cells(line)
            if cells[-1] != "Issue":
                cells.append("Issue")
            header_cols = len(cells)
            expect_separator = True
            out.append(_format_row(cells))
            continue
        if expect_separator and line.startswith("|---"):
            expect_separator = False
            assert header_cols is not None
            out.append("|" + "---|" * header_cols)
            continue
        expect_separator = False
        match = PROGRESS_ROW_RE.match(line)
        if match and header_cols is not None:
            cells = _cells(line)
            base = cells[: header_cols - 1]
            if len(base) < header_cols - 1:
                base += [""] * (header_cols - 1 - len(base))
            previous = cells[header_cols - 1] if len(cells) >= header_cols else ""
            number = numbers.get(match.group(1))
            out.append(_format_row([*base, f"#{number}" if number else previous]))
            continue
        if not line.startswith("|"):
            header_cols = None
        out.append(line)
    return "\n".join(out) + "\n"


def write_issue_numbers(progress_path: Path, numbers: dict[str, int]) -> None:
    text = progress_path.read_text(encoding="utf-8")
    progress_path.write_text(add_issue_numbers(text, numbers), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import plan stories into GitHub issues.")
    parser.add_argument("plan_path", type=Path)
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would happen, no gh calls"
    )
    parser.add_argument("--update", action="store_true", help="rewrite issue bodies that differ")
    parser.add_argument("--repo", help="owner/name (default: the repo gh is set up for)")
    args = parser.parse_args(argv)
    try:
        numbers = import_backlog(args.plan_path, args.repo, None, args.dry_run, args.update)
    except (ValueError, GhError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not args.dry_run:
        write_issue_numbers(DEFAULT_PROGRESS, numbers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
