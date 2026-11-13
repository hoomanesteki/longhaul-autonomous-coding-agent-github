"""Write the Quarto pages and fragments that the docs site derives from docs/.

Quarto runs this before every render (`project.pre-render` in site/_quarto.yml). It reads
docs/ and writes only to two places that git ignores:

* site/adr/         one page per architecture decision record
* site/_generated/  fragments that the hand-written pages embed: the ADR index, the list of
                    gate reports, the phase status table, the diagrams and tables that live
                    in the build plan, and the chaos table and chart. It also holds a copy of
                    every image under docs/, at the same path, so the pages can show them, and
                    the map from each doc to its page, which site/filters/docs.lua uses to
                    point the links of an included doc at the site

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
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SITE_NAME = "Longhaul"
PLAN_FILE = Path("docs/plan/longhaul-build-plan.md")
PROGRESS_FILE = Path("docs/progress.md")
# The chaos run the README quotes. scripts/chaos_chart.py draws CHAOS_CHART from the same file.
CHAOS_SUMMARY = Path("eval/results/chaos-2026-11-07/summary.json")
CHAOS_CHART = Path("docs/media/chaos-results.svg")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# The site page of each doc, by its path in the repo. A link in an included doc that points at
# one of these goes to the page. Any other link to a file of the repo goes to GitHub. ADRs and
# gate reports are added by doc_pages(), because their pages follow the files in docs/.
DOC_PAGES = {
    "README.md": "index.qmd",
    "docs/architecture.md": "architecture.qmd",
    "docs/case-study.md": "case-study.qmd",
    "docs/demo-script.md": "demo.qmd",
    "docs/deploy-aws.md": "deploy-aws.qmd",
    "docs/plan/longhaul-build-plan.md": "plan.qmd",
    "docs/progress.md": "progress.qmd",
    "docs/results.md": "results.qmd",
    "docs/runbook.md": "runbook.qmd",
    "docs/security.md": "security.qmd",
}

# The kill types of the chaos summary, in the order and with the labels of the README table.
KILL_TYPES = (
    ("worker", "Worker SIGKILL"),
    ("sandbox", "Sandbox kill"),
    ("temporal", "Temporal restart"),
    ("postgres", "Postgres restart"),
)
# How the list of open stories under the status table words a status of docs/progress.md.
OPEN_STATUS = {"Todo": "not started"}
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
    # (id, title, status) of each story that is not done, in the order of the file.
    open_stories: tuple[tuple[str, str, str], ...] = ()

    @property
    def state(self) -> str:
        if self.done == self.stories:
            return "Done"
        return "In progress" if self.started else "Not started"


def read_phases(progress: str) -> list[Phase]:
    """One summary per phase table in docs/progress.md."""
    # Each entry holds a phase heading and the (id, title, points, status) of every story row
    # below it.
    entries: list[tuple[int, str, str, list[tuple[str, str, int, str]]]] = []
    for line in progress.splitlines():
        heading = PHASE_HEADING_RE.match(line)
        if heading:
            number, name, tag = heading.groups()
            entries.append((int(number), name, tag, []))
        elif entries and line.startswith("| LH-"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 4 or not cells[2].isdigit():
                raise SiteError(f"{PROGRESS_FILE}: cannot read the row {line!r}")
            entries[-1][3].append((cells[0], cells[1], int(cells[2]), cells[3]))
    if not entries:
        raise SiteError(f"{PROGRESS_FILE}: no '## Phase N: Name (tag vX.Y.Z)' headings")
    return [
        Phase(
            number=number,
            name=name,
            tag=tag,
            stories=len(stories),
            done=sum(status == "Done" for *_, status in stories),
            started=sum(status != "Todo" for *_, status in stories),
            points=sum(points for _, _, points, _ in stories),
            points_done=sum(points for _, _, points, status in stories if status == "Done"),
            open_stories=tuple(
                (story, title, status) for story, title, _, status in stories if status != "Done"
            ),
        )
        for number, name, tag, stories in entries
    ]


# --- rendering ------------------------------------------------------------------------------


def include(path: str) -> str:
    return "{{< include " + path + " >}}"


def source_note(doc: str) -> str:
    """A line that tells a reader where the page text lives. `source-repo` is in _quarto.yml.

    The pages put it at the end, where it does not stand between a reader and the text.
    """
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
    # pagetitle keeps Quarto from joining the title and the site name with an en dash in the
    # social card. The description is for search engines and the card, not for the page.
    description = (
        f"The decision record {adr.heading}: its context, the decision, its consequences "
        "and the alternatives that were rejected."
    )
    return (
        "---\n"
        f"title: {json.dumps(adr.heading, ensure_ascii=False)}\n"
        f"pagetitle: {json.dumps(adr.heading + ' | ' + SITE_NAME, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "hide-description: true\n"
        # Quarto reads the image path from the page's folder.
        "image: ../social-card.png\n"
        "---\n\n"
        # The ADR starts with its own H1, which the page title already shows. Its links are
        # relative to docs/adr/, and docs.lua points them at the site from there.
        '::: {.drop-title base="docs/adr"}\n'
        f"{include(doc_path)}\n"
        ":::\n\n"
        f"{source_note(f'docs/adr/{adr.stem}.md')}\n"
        f"::: {{.page-links}}\n{' · '.join(links)}\n:::\n"
    )


def adr_index(adrs: list[Adr], planned: list[tuple[str, str]]) -> str:
    rows = [
        "| ADR | Decision | Status | Date |",
        "|---|---|---|---|",
        *(
            f"| {a.number} | [{cell(a.title)}](adr/{a.stem}.qmd) | {cell(a.status)} | {a.date} |"
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
    text = (
        f"{done} of {stories} stories are done, which is {points_done} of {points} points, "
        f"across {len(phases)} phases.\n\n" + "\n".join(rows) + "\n"
    )
    still_open = [(p.number, story) for p in phases for story in p.open_stories]
    if still_open:
        text += "\nThe stories that are not done:\n\n" + "".join(
            f"- {story} {title} (Phase {number}, {OPEN_STATUS.get(status, status.lower())})\n"
            for number, (story, title, status) in still_open
        )
    return text


def doc_pages(adrs: list[Adr], gates: list[Path]) -> str:
    """The map from doc to page for site/filters/docs.lua: one "doc page" pair per line."""
    pages = dict(DOC_PAGES)
    pages.update({f"docs/adr/{a.stem}.md": f"adr/{a.stem}.qmd" for a in adrs})
    pages.update({f"docs/gates/{path.name}": "gates.qmd" for path in gates})
    return "".join(f"{doc} {page}\n" for doc, page in sorted(pages.items()))


def _seconds(value: float) -> str:
    return f"{value:.1f} s"


def chaos_table(summary: dict[str, Any]) -> str:
    """The chaos table of the README, row for row, built from the summary of the run.

    A run counts as hit by a kill type when a kill of that type landed in it.
    """
    results = summary["results"]
    header = (
        "| Kill type | Kills landed / planned | Runs hit | Completed | Diff equal "
        "| Duplicate side effects | Resume p50 / p95 |"
    )
    rows = [header, "|---|---|---|---|---|---|---|"]
    for kind, label in KILL_TYPES:
        hit = [r for r in results if any(k["type"] == kind and k["landed"] for k in r["kills"])]
        latency = summary["resume_latency_by_type"][kind]
        cells = [
            label,
            f"{summary['kills_landed'][kind]} / {summary['kills_planned'][kind]}",
            str(len(hit)),
            f"{sum(1 for r in hit if r['completed'])} / {len(hit)}",
            f"{sum(1 for r in hit if r['diff_equals_reference'])} / {len(hit)}",
            str(sum(r["duplicate_side_effects"] for r in hit)),
            f"{_seconds(latency['median_s'])} / {_seconds(latency['p95_s'])}",
        ]
        rows.append("| " + " | ".join(cells) + " |")
    hit_any = [r for r in results if any(k["landed"] for k in r["kills"])]
    overall, runs = summary["resume_latency"], summary["runs"]
    cells = [
        "**All**",
        f"{sum(summary['kills_landed'].values())} / {sum(summary['kills_planned'].values())}",
        str(len(hit_any)),
        f"{summary['completed']} / {runs}",
        f"{summary['diff_matches']} / {runs}",
        str(summary["duplicate_side_effects"]),
        f"{_seconds(overall['median_s'])} / {_seconds(overall['p95_s'])}",
    ]
    rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows) + "\n"


def chaos_chart(svg: str) -> str:
    """The committed chart as a raw HTML block, so the page can set its colors from the theme."""
    return '```{=html}\n<figure class="chaos-figure">\n' + svg.strip() + "\n</figure>\n```\n"


def doc_images(docs: Path) -> list[Path]:
    """Every image under docs/, which the pages show from their copies in site/_generated/."""
    return sorted(
        path for path in docs.rglob("*") if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
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
    summary = json.loads((root / CHAOS_SUMMARY).read_text(encoding="utf-8"))
    chart = (root / CHAOS_CHART).read_text(encoding="utf-8")

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
        "doc-pages.txt": doc_pages(adrs, gates),
        "chaos-table.md": chaos_table(summary),
        "chaos-chart.md": chaos_chart(chart),
    }
    for name, text in fragments.items():
        _write(generated / name, text)
        written.append(generated / name)
    # A page shows an image of docs/ from this copy, at the same path under _generated/.
    for image in doc_images(docs):
        copy = generated / image.relative_to(root)
        copy.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image, copy)
        written.append(copy)
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
