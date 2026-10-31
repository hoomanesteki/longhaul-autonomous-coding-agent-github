"""Write the Quarto pages and fragments that the docs site derives from docs/.

Quarto runs this before every render (`project.pre-render` in site/_quarto.yml). It reads
docs/ and writes only to two places that git ignores:

* site/adr/         one page per architecture decision record
* site/_generated/  fragments that the hand-written pages embed: the ADR index, the list of
                    gate reports, the phase status table, and the diagrams and tables that
                    live in the build plan

A new ADR or gate report therefore reaches the site with no edit under site/, and nothing is
copied by hand. The pages embed the fragments with site/filters/fragments.lua and not with the
include shortcode, because Quarto expands includes before this script has run.

Quarto starts this script with whatever python3 is on the PATH, not with the project's uv
environment. It uses only the standard library and stays valid on Python 3.9.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN_FILE = Path("docs/plan/longhaul-build-plan.md")
PROGRESS_FILE = Path("docs/progress.md")
# Pandoc turns the heading "3.4 Architecture decisions (ADR summaries)" into this anchor.
PLAN_ADR_LINK = "plan.qmd#architecture-decisions-adr-summaries"

ADR_FILE_RE = re.compile(r"^(\d{4})-.+\.md$")
ADR_HEADING_RE = re.compile(r"^# (ADR-(\d{4}): (.+))$", re.MULTILINE)
ADR_STATUS_RE = re.compile(r"^- Status: (.+)$", re.MULTILINE)
ADR_DATE_RE = re.compile(r"^- Date: (.+)$", re.MULTILINE)
PLANNED_ADR_RE = re.compile(r"^- \*\*ADR-(\d{4}) (.+?)\.\*\*", re.MULTILINE)
GATE_FILE_RE = re.compile(r"^phase-(\d+)\.md$")
PHASE_HEADING_RE = re.compile(r"^## Phase (\d+): (.+) \(tag (v[\d.]+)\)$")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,6}) ")


class SiteError(Exception):
    """A doc does not have the shape this script relies on."""


# --- reading the plan -----------------------------------------------------------------------


def _fence_run(line: str) -> str:
    """The backticks or tildes that open or close a fence on this line, or an empty string."""
    found = FENCE_RE.match(line)
    return found.group(1) if found else ""


def _closes(opening: str, line: str) -> bool:
    """Whether `line` closes a fence opened with `opening`.

    A closing fence repeats the opening character at least as often and carries no info
    string. That lets a four backtick fence hold three backtick lines.
    """
    run = _fence_run(line)
    return bool(run) and run[0] == opening[0] and len(run) >= len(opening) and line.strip() == run


def _prose_flags(lines: list[str]) -> list[bool]:
    """One flag per line: True for prose, False for a fence line or a line inside a fence.

    The plan holds shell snippets whose comments start with `#`, so a heading search that
    ignored fences would find headings that are not there.
    """
    flags: list[bool] = []
    opening = ""
    for line in lines:
        if opening:
            flags.append(False)
            if _closes(opening, line):
                opening = ""
        else:
            opening = _fence_run(line)
            flags.append(not opening)
    return flags


def section(text: str, number: str) -> list[str]:
    """The lines under the heading numbered `number`, for example "3.1", heading excluded.

    The section ends at the next heading of the same or a higher level.
    """
    lines = text.splitlines()
    prose = _prose_flags(lines)
    heading = re.compile(rf"^(#{{2,6}}) {re.escape(number)} ")
    for start, line in enumerate(lines):
        found = heading.match(line) if prose[start] else None
        if found:
            level = len(found.group(1))
            break
    else:
        raise SiteError(f"{PLAN_FILE}: no heading for section {number}")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        other = HEADING_RE.match(lines[index])
        if prose[index] and other and len(other.group(1)) <= level:
            end = index
            break
    return lines[start + 1 : end]


def mermaid_blocks(lines: list[str]) -> list[str]:
    """The text of every ```mermaid fence in `lines`, except one inside another fence."""
    blocks: list[str] = []
    opening = ""
    current: list[str] | None = None
    for line in lines:
        if not opening:
            opening = _fence_run(line)
            if opening:
                current = [] if line.strip() == f"{opening}mermaid" else None
        elif _closes(opening, line):
            if current is not None:
                blocks.append("\n".join(current) + "\n")
            opening, current = "", None
        elif current is not None:
            current.append(line)
    return blocks


def first_table(lines: list[str]) -> list[str]:
    """The rows of the first pipe table in the prose of `lines`, header and separator included."""
    prose = _prose_flags(lines)
    table: list[str] = []
    for index, line in enumerate(lines):
        if prose[index] and line.startswith("|"):
            table.append(line)
        elif table:
            break
    return table


def plan_diagram(plan: str, number: str) -> str:
    blocks = mermaid_blocks(section(plan, number))
    if len(blocks) != 1:
        raise SiteError(f"{PLAN_FILE}: section {number} must hold one mermaid diagram")
    return blocks[0]


def plan_table(plan: str, number: str) -> str:
    table = first_table(section(plan, number))
    if len(table) < 3:
        raise SiteError(f"{PLAN_FILE}: section {number} must hold a table with rows")
    return "\n".join(table) + "\n"


def planned_adrs(plan: str, written: set[str]) -> list[tuple[str, str]]:
    """(number, title) of each decision that section 3.4 names and has no record yet."""
    named = PLANNED_ADR_RE.findall("\n".join(section(plan, "3.4")))
    if not named:
        raise SiteError(f"{PLAN_FILE}: section 3.4 lists no ADR summaries")
    return [(number, title) for number, title in named if number not in written]


# --- reading docs/ --------------------------------------------------------------------------


@dataclass(frozen=True)
class Adr:
    number: str
    heading: str
    title: str
    status: str
    date: str
    stem: str


def adr_files(adr_dir: Path) -> list[Path]:
    """Every ADR in number order. 0000-template.md is the template, not a decision."""
    return sorted(
        path
        for path in adr_dir.glob("*.md")
        if (named := ADR_FILE_RE.match(path.name)) and named.group(1) != "0000"
    )


def read_adr(path: Path) -> Adr:
    text = path.read_text(encoding="utf-8")
    heading, status, date = (
        pattern.search(text) for pattern in (ADR_HEADING_RE, ADR_STATUS_RE, ADR_DATE_RE)
    )
    if not heading:
        raise SiteError(f"{path.name}: the first line must read '# ADR-NNNN: Title'")
    if heading.group(2) != path.name[:4]:
        raise SiteError(f"{path.name}: the heading says ADR-{heading.group(2)}")
    if not status:
        raise SiteError(f"{path.name}: no '- Status:' line")
    if not date:
        raise SiteError(f"{path.name}: no '- Date:' line")
    return Adr(
        number=heading.group(2),
        heading=heading.group(1),
        title=heading.group(3),
        status=status.group(1).strip(),
        date=date.group(1).strip(),
        stem=path.stem,
    )


def gate_files(gates_dir: Path) -> list[Path]:
    """Gate reports in phase order. A name that is not phase-N.md goes last."""

    def order(path: Path) -> tuple[int, str]:
        named = GATE_FILE_RE.match(path.name)
        return (int(named.group(1)) if named else 10**6, path.name)

    return sorted(gates_dir.glob("*.md"), key=order)


@dataclass(frozen=True)
class Phase:
    number: int
    name: str
    tag: str
    stories: int
    done: int
    started: int
    points: int
    points_done: int

    @property
    def state(self) -> str:
        if self.done == self.stories:
            return "Done"
        return "In progress" if self.started else "Not started"


def read_phases(progress: str) -> list[Phase]:
    """One summary per phase table in docs/progress.md."""
    # Each entry holds a phase heading and the (points, status) of every story row below it.
    entries: list[tuple[int, str, str, list[tuple[int, str]]]] = []
    for line in progress.splitlines():
        heading = PHASE_HEADING_RE.match(line)
        if heading:
            number, name, tag = heading.groups()
            entries.append((int(number), name, tag, []))
        elif entries and line.startswith("| LH-"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 4 or not cells[2].isdigit():
                raise SiteError(f"{PROGRESS_FILE}: cannot read the row {line!r}")
            entries[-1][3].append((int(cells[2]), cells[3]))
    if not entries:
        raise SiteError(f"{PROGRESS_FILE}: no '## Phase N: Name (tag vX.Y.Z)' headings")
    return [
        Phase(
            number=number,
            name=name,
            tag=tag,
            stories=len(stories),
            done=sum(status == "Done" for _, status in stories),
            started=sum(status != "Todo" for _, status in stories),
            points=sum(points for points, _ in stories),
            points_done=sum(points for points, status in stories if status == "Done"),
        )
        for number, name, tag, stories in entries
    ]


# --- rendering ------------------------------------------------------------------------------


def include(path: str) -> str:
    return "{{< include " + path + " >}}"


def source_note(doc: str) -> str:
    """A line that tells a reader where the page text lives. `source-repo` is in _quarto.yml."""
    url = "{{< meta source-repo >}}/blob/main/" + doc
    return (
        "::: {.source-note}\n"
        f"Rendered from [`{doc}`]({url}). To change it, edit that file.\n"
        ":::\n"
    )


def cell(text: str) -> str:
    return text.replace("|", "\\|")


def adr_page(adr: Adr, doc_path: str, before: Adr | None, after: Adr | None) -> str:
    """A page whose title and body both come from the ADR. `doc_path` is relative to the page.

    The way between ADRs is a line of plain links. A Quarto sidebar could list the pages, but
    its globs fail when the project sits under a dot folder such as .worktrees.
    """
    links = [f"[Previous: ADR-{before.number}]({before.stem}.qmd)"] if before else []
    links.append("[All decisions](../decisions.qmd)")
    if after:
        links.append(f"[Next: ADR-{after.number}]({after.stem}.qmd)")
    return (
        "---\n"
        f"title: {json.dumps(adr.heading, ensure_ascii=False)}\n"
        "---\n\n"
        f"{source_note(f'docs/adr/{adr.stem}.md')}\n"
        # The ADR starts with its own H1, which the page title already shows.
        "::: {.drop-title}\n"
        f"{include(doc_path)}\n"
        ":::\n\n"
        f"::: {{.page-links}}\n{' · '.join(links)}\n:::\n"
    )


def adr_index(adrs: list[Adr], planned: list[tuple[str, str]]) -> str:
    rows = [
        "| ADR | Decision | Status | Date |",
        "|---|---|---|---|",
        *(
            f"| [{a.number}](adr/{a.stem}.qmd) | {cell(a.title)} | {cell(a.status)} | {a.date} |"
            for a in adrs
        ),
    ]
    text = "## Decision records\n\n" + "\n".join(rows) + "\n"
    if planned:
        waiting = [
            "| ADR | Decision | Status |",
            "|---|---|---|",
            *(f"| {number} | {cell(title)} | Planned |" for number, title in planned),
        ]
        text += (
            "\n## Planned decisions\n\n"
            "The plan names these decisions before the code that needs them exists. Each one "
            "becomes a record, with the same number, in the pull request that builds it. The "
            f"summaries are in [section 3.4 of the plan]({PLAN_ADR_LINK}).\n\n"
            + "\n".join(waiting)
            + "\n"
        )
    return text


def gates_list(doc_paths: list[str]) -> str:
    """The gate reports, one path per line, for site/filters/fragments.lua to embed in order."""
    return "".join(f"{path}\n" for path in doc_paths)


def status_fragment(phases: list[Phase]) -> str:
    stories = sum(p.stories for p in phases)
    done = sum(p.done for p in phases)
    points = sum(p.points for p in phases)
    points_done = sum(p.points_done for p in phases)
    rows = [
        "| Phase | What it covers | Tag | Stories done | State |",
        "|---|---|---|---|---|",
        *(
            f"| {p.number} | {cell(p.name)} | {p.tag} | {p.done} of {p.stories} | {p.state} |"
            for p in phases
        ),
    ]
    return (
        f"{done} of {stories} stories are done, which is {points_done} of {points} points, "
        f"across {len(phases)} phases.\n\n" + "\n".join(rows) + "\n"
    )


# --- writing --------------------------------------------------------------------------------


def _relative(target: Path, start: Path) -> str:
    return Path(os.path.relpath(target, start)).as_posix()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(root: Path) -> list[Path]:
    """Write every derived page and fragment under root/site. Returns the paths written."""
    docs, site = root / "docs", root / "site"
    plan = (root / PLAN_FILE).read_text(encoding="utf-8")
    progress = (root / PROGRESS_FILE).read_text(encoding="utf-8")
    adr_docs = [(path, read_adr(path)) for path in adr_files(docs / "adr")]
    adrs = [adr for _, adr in adr_docs]
    planned = planned_adrs(plan, {a.number for a in adrs})
    phases = read_phases(progress)
    gates = gate_files(docs / "gates")

    adr_dir, generated = site / "adr", site / "_generated"
    # Both folders belong to this script. Clearing them drops pages of renamed or removed docs.
    shutil.rmtree(generated, ignore_errors=True)
    for stale in adr_dir.glob("*.qmd"):
        stale.unlink()

    written: list[Path] = []
    for index, (path, adr) in enumerate(adr_docs):
        before = adrs[index - 1] if index > 0 else None
        after = adrs[index + 1] if index + 1 < len(adrs) else None
        page = adr_dir / f"{adr.stem}.qmd"
        _write(page, adr_page(adr, _relative(path, adr_dir), before, after))
        written.append(page)

    # fragments.lua reads these paths from the site folder, so the gate paths start there.
    fragments = {
        "adr-index.md": adr_index(adrs, planned),
        "gates.txt": gates_list([_relative(path, site) for path in gates]),
        "status.md": status_fragment(phases),
        "system-context.mmd": plan_diagram(plan, "3.1"),
        "task-lifecycle.mmd": plan_diagram(plan, "3.2"),
        "components.md": plan_table(plan, "3.3"),
        "eval-questions.md": plan_table(plan, "8.1"),
    }
    for name, text in fragments.items():
        _write(generated / name, text)
        written.append(generated / name)
    return written


def main() -> int:
    try:
        written = build(ROOT)
    except SiteError as error:
        print(f"site_pages: {error}", file=sys.stderr)
        return 1
    print(f"site_pages: wrote {len(written)} files under site/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
