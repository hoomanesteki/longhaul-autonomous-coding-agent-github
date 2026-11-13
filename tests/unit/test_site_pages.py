"""scripts/site_pages.py: the pre-render step that turns docs/ into pages for the Quarto site."""

import ast
import html
import importlib.util
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("site_pages", ROOT / "scripts" / "site_pages.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses look their module up in sys.modules, so register it before running it.
    sys.modules["site_pages"] = module
    spec.loader.exec_module(module)
    return module


site_pages = _load()
SiteError = site_pages.SiteError

INCLUDE_RE = re.compile(r"\{\{< include (\S+) >\}\}")
# The attributes of a div that site/filters/fragments.lua replaces with a generated file.
FRAGMENT_RE = re.compile(r'\b(fragments?)="([^"]+)"')
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")

PLAN = """\
# Plan

### 3.1 First

one
```bash
# 3.2 Looks like a heading, but it sits in a fence
```
````markdown
```mermaid
inner
```
````

### 3.2 Second

two

## 4. Next level up

four
"""

ADR_TEXT = """\
# ADR-{number}: {title}

- Status: {status}
- Date: 2026-10-30

## Context

Text.
"""


def _adr(folder: Path, number: str = "0099", slug: str = "example", **fields: str) -> Path:
    values = {"title": "Use a queue", "status": "Accepted", **fields}
    path = folder / f"{number}-{slug}.md"
    path.write_text(ADR_TEXT.format(number=number, **values), encoding="utf-8")
    return path


# --- reading the plan -----------------------------------------------------------------------


def test_section_runs_to_the_next_heading_and_ignores_headings_in_fences() -> None:
    body = site_pages.section(PLAN, "3.1")
    assert body[:2] == ["", "one"]
    assert "# 3.2 Looks like a heading, but it sits in a fence" in body
    assert site_pages.section(PLAN, "3.2") == ["", "two", ""]


def test_a_diagram_inside_another_fence_is_not_a_diagram() -> None:
    # The plan shows Markdown samples in four backtick fences. A ```mermaid line in one of
    # them is part of the sample.
    assert site_pages.mermaid_blocks(site_pages.section(PLAN, "3.1")) == []


def test_section_fails_when_the_heading_is_gone() -> None:
    with pytest.raises(SiteError, match="section 9.9"):
        site_pages.section(PLAN, "9.9")


def test_mermaid_blocks_and_first_table() -> None:
    lines = ["intro", "```mermaid", "flowchart LR", "  A --> B", "```", "| a | b |", "|---|---|"]
    lines += ["| 1 | 2 |", "", "| other | table |"]
    assert site_pages.mermaid_blocks(lines) == ["flowchart LR\n  A --> B\n"]
    assert site_pages.first_table(lines) == ["| a | b |", "|---|---|", "| 1 | 2 |"]


def test_a_table_inside_a_fence_is_not_a_table() -> None:
    lines = ["```text", "| a | b |", "```", "| real | table |", "|---|---|", "| 1 | 2 |"]
    assert site_pages.first_table(lines)[0] == "| real | table |"


@pytest.mark.parametrize("body", ["no diagram here", "```mermaid\nA\n```\n```mermaid\nB\n```"])
def test_plan_diagram_wants_exactly_one_diagram(body: str) -> None:
    with pytest.raises(SiteError, match="one mermaid diagram"):
        site_pages.plan_diagram(f"### 3.1 System\n\n{body}\n", "3.1")


def test_plan_table_wants_a_table_with_rows() -> None:
    with pytest.raises(SiteError, match="a table with rows"):
        site_pages.plan_table("### 8.1 Questions\n\n| a | b |\n|---|---|\n", "8.1")


def test_planned_adrs_leave_out_decisions_that_have_a_record() -> None:
    plan = (
        "### 3.4 Decisions\n\n"
        "- **ADR-0001 Record decisions as ADRs.** Every choice.\n"
        "- **ADR-0002 Pin dependencies.** Pin them.\n"
        "- **ADR-0003 Poll, don't webhook (v1).** A schedule.\n"
    )
    assert site_pages.planned_adrs(plan, {"0001"}) == [
        ("0002", "Pin dependencies"),
        ("0003", "Poll, don't webhook (v1)"),
    ]
    assert site_pages.planned_adrs(plan, {"0001", "0002", "0003"}) == []


def test_planned_adrs_fail_when_the_plan_lists_none() -> None:
    with pytest.raises(SiteError, match="no ADR summaries"):
        site_pages.planned_adrs("### 3.4 Decisions\n\nNothing here.\n", set())


# --- reading docs/ --------------------------------------------------------------------------


def test_read_adr_takes_title_status_and_date_from_the_file(tmp_path: Path) -> None:
    adr = site_pages.read_adr(_adr(tmp_path, status="Superseded by ADR-0100"))
    assert (adr.number, adr.title, adr.status, adr.date) == (
        "0099",
        "Use a queue",
        "Superseded by ADR-0100",
        "2026-10-30",
    )
    assert adr.heading == "ADR-0099: Use a queue"
    assert adr.stem == "0099-example"


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda t: t.replace("- Status: Accepted\n", ""), "no '- Status:' line"),
        (lambda t: t.replace("- Date: 2026-10-30\n", ""), "no '- Date:' line"),
        (lambda t: t.replace("# ADR-0099: ", "# "), "first line must read"),
        (lambda t: t.replace("# ADR-0099", "# ADR-0005"), "the heading says ADR-0005"),
    ],
    ids=["status", "date", "heading", "number"],
)
def test_read_adr_names_the_problem(
    tmp_path: Path, edit: Callable[[str], str], message: str
) -> None:
    path = _adr(tmp_path)
    path.write_text(edit(path.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(SiteError, match=re.escape(message)):
        site_pages.read_adr(path)


def test_adr_files_skip_the_template_and_other_files(tmp_path: Path) -> None:
    for name in ("0000-template.md", "0002-b.md", "0001-a.md", "notes.md", "0003-c.txt"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert [p.name for p in site_pages.adr_files(tmp_path)] == ["0001-a.md", "0002-b.md"]


def test_gate_files_come_in_phase_order_and_odd_names_last(tmp_path: Path) -> None:
    for name in ("phase-10.md", "notes.md", "phase-2.md", "phase-0.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert [p.name for p in site_pages.gate_files(tmp_path)] == [
        "phase-0.md",
        "phase-2.md",
        "phase-10.md",
        "notes.md",
    ]


PROGRESS = """\
# Progress

## Phase 0: Foundation (tag v0.0.1)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-001 | Scaffold | 3 | Done | merge | 2026-10-25 |
| LH-002 | Stack | 2 | In progress | | |

## Phase 1: Core (tag v0.1.0)
| ID | Title | Pts | Status | PR | Merged |
|---|---|---|---|---|---|
| LH-101 | Contracts | 5 | Todo | | |
"""


def test_read_phases_counts_stories_and_points() -> None:
    first, second = site_pages.read_phases(PROGRESS)
    assert (first.number, first.name, first.tag) == (0, "Foundation", "v0.0.1")
    assert (first.stories, first.done, first.points, first.points_done) == (2, 1, 5, 3)
    assert first.state == "In progress"
    assert (second.stories, second.done, second.state) == (1, 0, "Not started")


def test_read_phases_keeps_the_stories_that_are_not_done() -> None:
    first, second = site_pages.read_phases(PROGRESS)
    assert first.open_stories == (("LH-002", "Stack", "In progress"),)
    assert second.open_stories == (("LH-101", "Contracts", "Todo"),)


def test_the_strip_lists_the_stories_that_are_not_done() -> None:
    strip = site_pages.phase_strip(site_pages.read_phases(PROGRESS))
    assert strip.endswith(
        "The stories that are not done:\n\n"
        "- LH-002 Stack (Phase 0, in progress)\n"
        "- LH-101 Contracts (Phase 1, not started)\n"
    )
    # A phase that is not done says so in words, not by the color of its bar alone.
    assert '<span class="badge-status active">In progress</span>' in strip
    assert '<span class="badge-status idle">Not started</span>' in strip
    assert '<span style="width: 50%">' in strip
    assert "Every phase is done" not in strip
    done = site_pages.read_phases(PROGRESS.replace("In progress", "Done").replace("Todo", "Done"))
    finished = site_pages.phase_strip(done)
    assert "not done" not in finished
    assert "badge-status" not in finished
    assert "3 of 3 stories are done, which is 10 of 10 points, across 2 phases." in finished
    assert "Every phase is done." in finished


def test_the_strip_is_a_list_a_screen_reader_can_read() -> None:
    strip = site_pages.phase_strip(site_pages.read_phases(PROGRESS))
    assert strip.startswith("```{=html}\n")
    assert '<ol class="phase-list" aria-label="Stories done in each phase">' in strip
    assert strip.count('<li class="phase-item">') == 2
    # The bar is decoration. The words beside it carry the same numbers.
    assert strip.count('<span class="phase-bar" aria-hidden="true">') == 2
    item = re.sub(r"<[^>]+>", "", strip.split('<li class="phase-item">')[1].split("</li>")[0])
    assert item == "Phase 0 Foundation v0.0.1 1 of 2 stories done In progress"


def test_the_strip_escapes_a_phase_name() -> None:
    (phase,) = site_pages.read_phases(
        PROGRESS.split("\n## Phase 1")[0].replace("Foundation", "A <b> & C")
    )
    assert "A &lt;b&gt; &amp; C" in site_pages.phase_strip([phase])


def test_a_phase_with_every_story_done_is_done() -> None:
    done = PROGRESS.replace("In progress", "Done")
    assert site_pages.read_phases(done)[0].state == "Done"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# Progress\n", "no '## Phase N"),
        (PROGRESS.replace("| 3 |", "| three |"), "cannot read the row"),
    ],
)
def test_read_phases_names_the_problem(text: str, message: str) -> None:
    with pytest.raises(SiteError, match=re.escape(message)):
        site_pages.read_phases(text)


# --- rendering ------------------------------------------------------------------------------


def _record(number: str, title: str = "T") -> Any:
    return site_pages.Adr(number, f"ADR-{number}: {title}", title, "Accepted", "d", f"{number}-t")


def test_adr_page_title_carries_a_colon_and_quotes_safely() -> None:
    adr = site_pages.Adr("0004", 'ADR-0004: A "quoted": title', "A", "Accepted", "d", "0004-x")
    page = site_pages.adr_page(adr, "../../docs/adr/0004-x.md", None, None)
    assert page.splitlines()[1] == 'title: "ADR-0004: A \\"quoted\\": title"'


def test_adr_page_includes_the_doc_and_drops_its_own_heading() -> None:
    page = site_pages.adr_page(_record("0004"), "../../docs/adr/0004-t.md", None, None)
    # base names the folder of the doc, so docs.lua can point its relative links at the site.
    include = "{{< include ../../docs/adr/0004-t.md >}}"
    assert f'::: {{.drop-title base="docs/adr"}}\n{include}\n:::' in page
    assert "{{< meta source-repo >}}/blob/main/docs/adr/0004-t.md" in page


def test_adr_page_names_its_source_after_the_record() -> None:
    # The note is for whoever edits the page, so it comes after the text and not before it.
    page = site_pages.adr_page(_record("0004"), "../../docs/adr/0004-t.md", None, None)
    assert page.index("{{< include") < page.index(".source-note") < page.index(".page-links")


def test_adr_page_sets_the_titles_and_the_image_of_its_social_card() -> None:
    # Without pagetitle Quarto joins the title and the site name with an en dash in the card,
    # and it reads the image path from the page's folder, which is adr/.
    page = site_pages.adr_page(_record("0004", "Temporal"), "x.md", None, None)
    front = page.split("---\n")[1]
    assert 'pagetitle: "ADR-0004: Temporal | Longhaul"\n' in front
    assert 'description: "The decision record ADR-0004: Temporal:' in front
    assert "hide-description: true\n" in front
    assert "image: ../social-card.png\n" in front
    assert (ROOT / "site" / "social-card.png").is_file()


def test_adr_page_links_only_the_neighbours_that_exist() -> None:
    only = site_pages.adr_page(_record("0001"), "x.md", None, None)
    middle = site_pages.adr_page(_record("0002"), "x.md", _record("0001"), _record("0003"))
    assert "Previous" not in only
    assert "Next" not in only
    assert "[All decisions](../decisions.qmd)" in only
    assert "[Previous: ADR-0001](0001-t.qmd)" in middle
    assert "[Next: ADR-0003](0003-t.qmd)" in middle


def test_index_cells_escape_the_table_separator() -> None:
    adr = site_pages.Adr("0001", "ADR-0001: A | B", "A | B", "Accepted", "2026-10-25", "0001-a")
    assert "A \\| B" in site_pages.adr_index([adr], [])


def test_the_gate_list_has_one_path_per_line() -> None:
    assert site_pages.gates_list([]) == ""
    paths = ["../docs/gates/phase-0.md", "../docs/gates/phase-1.md"]
    assert site_pages.gates_list(paths) == "../docs/gates/phase-0.md\n../docs/gates/phase-1.md\n"


# --- build() on a copy of the real docs -----------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of docs/ with an empty site/, so build() never writes into the repo."""
    shutil.copytree(ROOT / "docs", tmp_path / "docs")
    for source in (site_pages.CHAOS_SUMMARY, site_pages.CI_WORKFLOW):
        copy = tmp_path / source
        copy.parent.mkdir(parents=True)
        shutil.copy(ROOT / source, copy)
    (tmp_path / "site").mkdir()
    return tmp_path


def _generated(tree: Path) -> dict[str, str]:
    folder = tree / "site" / "_generated"
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir()) if p.is_file()}


def test_build_writes_a_page_for_every_adr_and_none_for_the_template(tree: Path) -> None:
    site_pages.build(tree)
    pages = sorted(p.stem for p in (tree / "site" / "adr").glob("*.qmd"))
    records = sorted(p.stem for p in (ROOT / "docs" / "adr").glob("*.md") if p.name[:4] != "0000")
    assert pages == records
    assert records, "the repo should hold at least one ADR"


def test_every_path_the_script_writes_points_at_a_real_file(tree: Path) -> None:
    site_pages.build(tree)
    site = tree / "site"
    # An ADR page is rendered from site/adr, so its include is relative to that folder.
    for page in (site / "adr").glob("*.qmd"):
        for target in INCLUDE_RE.findall(page.read_text(encoding="utf-8")):
            assert (page.parent / target).resolve().is_file(), f"{page.name}: {target}"
    # fragments.lua reads the gate list from the site folder, so its paths start there.
    for target in _generated(tree)["gates.txt"].splitlines():
        assert (site / target).resolve().is_file(), target


def test_a_new_adr_reaches_the_site_without_an_edit_under_site(tree: Path) -> None:
    site_pages.build(tree)
    _adr(tree / "docs" / "adr", number="0099", slug="use-a-queue", title="Use a queue")
    site_pages.build(tree)

    page = (tree / "site" / "adr" / "0099-use-a-queue.qmd").read_text(encoding="utf-8")
    assert 'title: "ADR-0099: Use a queue"' in page
    index = _generated(tree)["adr-index.md"]
    assert "| 0099 | [Use a queue](adr/0099-use-a-queue.qmd) | Accepted | 2026-10-30 |" in index
    # The last record is the one before it, so its page links forward to the new one.
    previous = sorted((tree / "site" / "adr").glob("*.qmd"))[-2]
    assert "[Next: ADR-0099](0099-use-a-queue.qmd)" in previous.read_text(encoding="utf-8")


def test_a_removed_adr_loses_its_page(tree: Path) -> None:
    path = _adr(tree / "docs" / "adr", slug="short-lived")
    site_pages.build(tree)
    assert (tree / "site" / "adr" / "0099-short-lived.qmd").exists()
    path.unlink()
    site_pages.build(tree)
    assert not (tree / "site" / "adr" / "0099-short-lived.qmd").exists()


def test_a_new_gate_report_reaches_the_site(tree: Path) -> None:
    site_pages.build(tree)
    before = _generated(tree)["gates.txt"].splitlines()
    assert before, "the repo should hold at least one gate report"
    report = "# Phase 98 gate: Later\n\n- Date: 2026-12-01\n- Result: **Pass.**\n"
    (tree / "docs" / "gates" / "phase-98.md").write_text(report, encoding="utf-8")
    site_pages.build(tree)
    assert _generated(tree)["gates.txt"].splitlines() == [*before, "../docs/gates/phase-98.md"]
    assert (
        "| [98: Later](#phase-98-gate-later) |  |  | 2026-12-01 | Pass |"
        in _generated(tree)["gates-summary.md"]
    )


def test_the_gate_summary_names_each_phase_and_its_stories_as_progress_does() -> None:
    gate = site_pages.Gate(phase=7, heading="Phase 7 gate: Old", date="2026-11-08", result="Pass")
    phase = site_pages.Phase(7, "New name", "v0.7.0", 2, 2, 2, 5, 5)
    row = site_pages.gates_summary([gate], [phase]).splitlines()[2]
    assert row == "| [7: New name](#phase-7-gate-old) | v0.7.0 | Done | 2026-11-08 | Pass |"


def test_the_phase_strip_matches_the_progress_file(tree: Path) -> None:
    site_pages.build(tree)
    progress = (ROOT / "docs" / "progress.md").read_text(encoding="utf-8")
    strip = _generated(tree)["phase-strip.md"]
    # Read the file again here, apart from read_phases, so a mistake in it cannot hide.
    headings = re.findall(r"^## Phase (\d+): (.+) \(tag (v[\d.]+)\)$", progress, re.MULTILINE)
    sections = re.split(r"^## Phase \d+: .+$", progress, flags=re.MULTILINE)[1:]
    assert len(headings) == len(sections) == strip.count('<li class="phase-item">')
    total = done = 0
    for (number, name, tag), body in zip(headings, sections, strict=True):
        statuses = [
            line.split("|")[4].strip() for line in body.splitlines() if line.startswith("| LH-")
        ]
        finished = statuses.count("Done")
        total, done = total + len(statuses), done + finished
        item = (
            f'<span class="visually-hidden">Phase </span>{number}</span> '
            f'<span class="phase-name">{html.escape(name)}</span></span> '
            f'<span class="phase-meta"><code>{tag}</code> '
            f"<span>{finished} of {len(statuses)} stories done</span>"
        )
        assert item in strip, name
    assert f"{done} of {total} stories are done" in strip


def test_diagrams_and_tables_are_the_plans_own_text(tree: Path) -> None:
    site_pages.build(tree)
    generated = _generated(tree)
    plan = (ROOT / "docs" / "plan" / "longhaul-build-plan.md").read_text(encoding="utf-8")
    assert generated["system-context.mmd"].startswith("flowchart LR\n")
    assert generated["task-lifecycle.mmd"].startswith("sequenceDiagram\n")
    assert generated["components.md"].startswith("| Component | Package | Responsibility |")
    assert generated["eval-questions.md"].startswith("| # | Question | Evidence |")
    for name in ("system-context.mmd", "task-lifecycle.mmd", "components.md", "eval-questions.md"):
        assert generated[name].strip() in plan, name


def _pulled_in(page: Path) -> list[Path]:
    """Every file `page` includes with the include shortcode, resolved the way Quarto does.

    Quarto resolves an include from the folder of the page being rendered, also when the
    include sits inside an included file.
    """
    found: list[Path] = []
    pending = [page]
    while pending:
        for target in INCLUDE_RE.findall(pending.pop().read_text(encoding="utf-8")):
            path = (page.parent / target).resolve()
            assert path.is_file(), f"{page.name} includes {target}, which does not exist"
            found.append(path)
            if path.suffix == ".md":
                pending.append(path)
    return found


def _embedded(page: Path, site: Path) -> list[Path]:
    """Every file `page` embeds with fragments.lua, the entries of a list included.

    The filter reads paths from the project folder, which is `site`.
    """
    found: list[Path] = []
    for key, name in FRAGMENT_RE.findall(page.read_text(encoding="utf-8")):
        path = (site / name).resolve()
        assert path.is_file(), f"{page.name} embeds {name}, which does not exist"
        if key == "fragments":
            for entry in path.read_text(encoding="utf-8").splitlines():
                listed = (site / entry).resolve()
                assert listed.is_file(), f"{name} lists {entry}, which does not exist"
                found.append(listed)
        else:
            found.append(path)
    return found


@pytest.fixture
def site_tree(tree: Path) -> Path:
    """`tree` with the committed pages of site/ copied in and the script run, as Quarto would."""
    for page in (ROOT / "site").glob("*.qmd"):
        shutil.copy(page, tree / "site" / page.name)
    site_pages.build(tree)
    return tree


def test_every_include_and_fragment_in_every_page_resolves(site_tree: Path) -> None:
    site = site_tree / "site"
    pages = [*site.glob("*.qmd"), *(site / "adr").glob("*.qmd")]
    assert len(pages) >= 9
    for page in pages:
        # Both helpers fail on the first path that points at nothing. Every page uses one,
        # except the console page, which is screenshots and text (checked further down), and
        # the 404 page, which is a few links.
        if page.name == "404.qmd":
            continue
        shows = IMAGE_RE.findall(page.read_text(encoding="utf-8"))
        assert _pulled_in(page) or _embedded(page, site) or shows, page.name


def test_pages_embed_the_files_the_script_writes_and_do_not_include_them() -> None:
    # Quarto expands the include shortcode while it scans the project, before the pre-render
    # script has run. An include of a generated file stops the render on a clean checkout, where
    # the file does not exist yet. fragments.lua reads such files later.
    for page in (ROOT / "site").glob("*.qmd"):
        for target in INCLUDE_RE.findall(page.read_text(encoding="utf-8")):
            assert "_generated" not in Path(target).parts, f"{page.name} includes {target}"


def test_the_pages_pull_in_the_docs_they_stand_for(site_tree: Path) -> None:
    site = site_tree / "site"
    docs = site_tree / "docs"
    # Every doc with a page of its own, except the README, which the overview retells.
    included = {
        page: site_tree / doc for doc, page in site_pages.DOC_PAGES.items() if doc != "README.md"
    }
    assert {
        "architecture.qmd",
        "case-study.qmd",
        "demo.qmd",
        "deploy-aws.qmd",
        "plan.qmd",
        "progress.qmd",
        "results.qmd",
        "runbook.qmd",
        "security.qmd",
    } == set(included)
    for name, doc in included.items():
        assert doc.resolve() in _pulled_in(site / name), name
    assert (docs / "gates" / "phase-0.md").resolve() in _embedded(site / "gates.qmd", site)
    # Each ADR page pulls in exactly its own record.
    for page in (site / "adr").glob("*.qmd"):
        assert _pulled_in(page) == [(docs / "adr" / f"{page.stem}.md").resolve()]


def test_the_architecture_cells_draw_the_files_the_script_writes(site_tree: Path) -> None:
    text = (site_tree / "site" / "architecture.qmd").read_text(encoding="utf-8")
    cells = re.findall(r"^```\{mermaid\}\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)
    assert len(cells) == 2
    for cell in cells:
        (name,) = re.findall(r"^%%\| file: (\S+)$", cell, re.MULTILINE)
        assert (site_tree / "site" / name).is_file(), name


def test_build_gives_the_same_files_twice(tree: Path) -> None:
    site_pages.build(tree)
    first = {p: p.read_bytes() for p in (tree / "site").rglob("*") if p.is_file()}
    site_pages.build(tree)
    second = {p: p.read_bytes() for p in (tree / "site").rglob("*") if p.is_file()}
    assert first == second


def test_main_stops_the_render_with_a_message(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tree / "docs" / "adr" / "0001-record-decisions.md"
    path.write_text(path.read_text(encoding="utf-8").replace("- Status:", "- State:"), "utf-8")
    monkeypatch.setattr(site_pages, "ROOT", tree)
    assert site_pages.main() == 1
    assert "0001-record-decisions.md: no '- Status:' line" in capsys.readouterr().err


def test_main_reports_what_it_wrote(
    tree: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(site_pages, "ROOT", tree)
    assert site_pages.main() == 0
    assert re.search(r"site_pages: wrote \d+ files under site/", capsys.readouterr().out)


def test_the_script_stays_valid_on_python_3_9() -> None:
    # Quarto starts it with whatever python3 is on the PATH, which can be older than the
    # project's 3.12. Parsing with an older feature version catches syntax that needs 3.10, and
    # the future import keeps `X | None` annotations from being evaluated.
    source = (ROOT / "scripts" / "site_pages.py").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 9))
    assert "from __future__ import annotations" in source


# --- links, images and the chaos results ---------------------------------------------------

# A div may also carry mermaid="code" (plan.qmd), which site/filters/docs.lua reads.
DROP_TITLE_RE = re.compile(
    r"^::: \{\.drop-title(?: base=\"([^\"]+)\")?(?: mermaid=\"code\")?\}$", re.MULTILINE
)


def test_every_doc_in_the_map_exists_and_has_its_page() -> None:
    for doc, page in site_pages.DOC_PAGES.items():
        assert (ROOT / doc).is_file(), doc
        assert (ROOT / "site" / page).is_file(), page


def test_the_map_sends_adrs_and_gate_reports_to_their_pages(tree: Path) -> None:
    site_pages.build(tree)
    lines = _generated(tree)["doc-pages.txt"].splitlines()
    pairs = dict(line.split(" ") for line in lines)
    assert pairs["docs/case-study.md"] == "case-study.qmd"
    for adr in site_pages.adr_files(tree / "docs" / "adr"):
        assert pairs[f"docs/adr/{adr.name}"] == f"adr/{adr.stem}.qmd"
    for gate in site_pages.gate_files(tree / "docs" / "gates"):
        assert pairs[f"docs/gates/{gate.name}"] == "gates.qmd"


def test_every_included_doc_names_its_folder_so_its_links_reach_the_site(site_tree: Path) -> None:
    # Without base, a relative link or image of the doc would resolve from site/ and break.
    site = site_tree / "site"
    for page in [*site.glob("*.qmd"), *(site / "adr").glob("*.qmd")]:
        text = page.read_text(encoding="utf-8")
        targets = INCLUDE_RE.findall(text)
        if not targets:
            continue
        bases = DROP_TITLE_RE.findall(text)
        assert len(bases) == len(targets), page.name
        for base, target in zip(bases, targets, strict=True):
            doc = (page.parent / target).resolve()
            assert base == doc.parent.relative_to(site_tree).as_posix(), page.name


def test_every_image_under_docs_is_copied_for_the_site(tree: Path) -> None:
    site_pages.build(tree)
    images = site_pages.doc_images(tree / "docs")
    assert any("phase-8" in p.parts for p in images)
    for image in images:
        copy = tree / "site" / "_generated" / image.relative_to(tree)
        assert copy.read_bytes() == image.read_bytes(), image.name


def test_every_image_a_page_shows_exists_after_the_build(site_tree: Path) -> None:
    site = site_tree / "site"
    shown = 0
    for page in site.glob("*.qmd"):
        for target in IMAGE_RE.findall(page.read_text(encoding="utf-8")):
            assert (page.parent / target).is_file(), f"{page.name}: {target}"
            shown += 1
    assert shown >= 5, "the overview and the console page show the console screenshots"


def test_the_images_of_the_gate_reports_exist_beside_them() -> None:
    # docs.lua points them at their copies, which only exist when the originals do.
    for report in (ROOT / "docs" / "gates").glob("*.md"):
        for target in IMAGE_RE.findall(report.read_text(encoding="utf-8")):
            assert (report.parent / target).is_file(), f"{report.name}: {target}"


def _readme_chaos_rows() -> list[str]:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    return [line.replace("**", "") for line in readme.splitlines() if line.startswith("| ")]


def test_the_chaos_table_of_the_site_is_the_table_of_the_readme(tree: Path) -> None:
    site_pages.build(tree)
    rows = [
        line.replace("**", "")
        for line in _generated(tree)["chaos-table.md"].splitlines()
        if line.startswith("| ")
    ]
    assert len(rows) == 1 + len(site_pages.KILL_TYPES) + 1
    readme = _readme_chaos_rows()
    for row in rows:
        assert row in readme, row


def test_the_chaos_table_follows_the_summary(tree: Path) -> None:
    import json

    path = tree / site_pages.CHAOS_SUMMARY
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["kills_landed"]["worker"] = 7
    summary["duplicate_side_effects"] = 3
    path.write_text(json.dumps(summary), encoding="utf-8")
    site_pages.build(tree)
    table = _generated(tree)["chaos-table.md"]
    assert "| Worker SIGKILL | 7 / 10 |" in table
    assert table.splitlines()[-1].split(" | ")[5] == "3"


def test_the_chaos_chart_of_the_site_is_the_committed_chart(tree: Path) -> None:
    site_pages.build(tree)
    chart = _generated(tree)["chaos-chart.md"]
    assert chart.startswith("```{=html}\n")
    assert (ROOT / site_pages.CHAOS_CHART).read_text(encoding="utf-8").strip() in chart


def test_the_tiles_match_the_chaos_summary(tree: Path) -> None:
    import json

    site_pages.build(tree)
    summary = json.loads((ROOT / site_pages.CHAOS_SUMMARY).read_text(encoding="utf-8"))
    tiles = _generated(tree)["chaos-tiles.md"]
    values = dict(re.findall(r'<dt>([^<]+)</dt><dd class="stat-value">([^<]+)</dd>', tiles))
    runs = summary["runs"]
    landed = sum(summary["kills_landed"].values())
    planned = sum(summary["kills_planned"].values())
    assert values == {
        "Runs that ended with a pull request": f"{summary['completed']} of {runs}",
        "Duplicate GitHub writes": str(summary["duplicate_side_effects"]),
        "Kills that landed": f"{landed} of {planned}",
        "Median resume after a kill": f"{summary['resume_latency']['median_s']:.1f} s",
    }
    assert f"{summary['diff_matches']} of {runs} with the diff of a run with no kill" in tiles
    assert f"{summary['missing_side_effects']} missing" in tiles
    assert f"95th percentile {summary['resume_latency']['p95_s']:.1f}&nbsp;s" in tiles
    assert set(summary["recordings"].values()) == {"scripted"}
    assert "The model answers were scripted" in tiles


def test_the_tiles_follow_the_summary(tree: Path) -> None:
    import json

    path = tree / site_pages.CHAOS_SUMMARY
    summary = json.loads(path.read_text(encoding="utf-8"))
    summary["completed"] = 17
    summary["recordings"]["calc-01"] = "recorded"
    path.write_text(json.dumps(summary), encoding="utf-8")
    site_pages.build(tree)
    tiles = _generated(tree)["chaos-tiles.md"]
    assert '<dd class="stat-value">17 of 20</dd>' in tiles
    assert "scripted" not in tiles


def test_the_overview_and_results_show_the_tiles_and_type_no_chaos_number() -> None:
    # The headline numbers come from the summary at build time. A number typed into a page
    # would stay behind when a new run changes the file.
    for name in ("index.qmd", "results.qmd"):
        text = (ROOT / "site" / name).read_text(encoding="utf-8")
        assert '::: {fragment="_generated/chaos-tiles.md"}' in text, name
    overview = (ROOT / "site" / "index.qmd").read_text(encoding="utf-8")
    for typed in ("20 of 20", "39 of 45", "2.0 s", "18 runs", "22.1 s"):
        assert typed not in overview, typed
    assert '::: {fragment="_generated/chaos-chart.md"}' in overview
    assert '::: {fragment="_generated/phase-strip.md"}' in overview
    assert "status.md" not in overview


def test_the_results_page_says_what_each_row_of_the_chaos_table_counts() -> None:
    # The rows per kill type count the runs that a kill of that type hit, but the All row counts
    # every run, also the ones that no kill hit. A note that says otherwise reads 20 / 20 next to
    # 18 runs hit as a mistake.
    import json

    summary = json.loads((ROOT / site_pages.CHAOS_SUMMARY).read_text(encoding="utf-8"))
    planned = {len(r["kills_planned"]) for r in summary["results"]}
    assert (min(planned), max(planned)) == (1, 3)
    missed = sum(1 for r in summary["results"] if not any(k["landed"] for k in r["kills"]))
    assert missed == 2
    results = (ROOT / "site" / "results.qmd").read_text(encoding="utf-8")
    assert "Each run has one to three kills planned" in results
    assert "is hit with one to three kills" not in results
    assert "The **All** row counts every run, including the two that no kill hit." in results
    assert "count the runs that a kill of that type hit." not in results
    assert "in two runs none of them landed before the run ended" in results


# --- the CI diagram and the gate summary ----------------------------------------------------

WORKFLOW = """\
name: ci
on:
  push:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: echo "needs: [not, a, field]"
  # A comment between jobs.
  unit:
    needs: lint
  e2e:
    needs:
      - lint
      - "unit"
  nightly:
    if: github.event_name == 'schedule'
  ci:
    if: always()
    needs: [lint, unit, 'e2e']

other: value
"""


def test_read_ci_jobs_reads_every_form_of_needs() -> None:
    jobs = {job.name: job for job in site_pages.read_ci_jobs(WORKFLOW)}
    assert list(jobs) == ["lint", "unit", "e2e", "nightly", "ci"]
    assert jobs["lint"].needs == ()
    assert jobs["unit"].needs == ("lint",)
    assert jobs["e2e"].needs == ("lint", "unit")
    assert jobs["ci"].needs == ("lint", "unit", "e2e")
    assert jobs["nightly"].condition == "github.event_name == 'schedule'"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name: ci\n", "no top-level 'jobs:' key"),
        ("jobs:\nother: 1\n", "no jobs under 'jobs:'"),
        ("jobs:\n  ci:\n    needs: [gone]\n", "job ci needs gone, which is not a job"),
    ],
)
def test_read_ci_jobs_names_the_problem(text: str, message: str) -> None:
    with pytest.raises(SiteError, match=re.escape(message)):
        site_pages.read_ci_jobs(text)


def test_the_ci_diagram_draws_each_need_and_sets_a_lone_job_apart() -> None:
    diagram = site_pages.ci_diagram(site_pages.read_ci_jobs(WORKFLOW))
    assert diagram.splitlines()[1] == "flowchart LR"
    assert "  job0 --> job1\n" in diagram
    assert "  job0 & job1 --> job2\n" in diagram
    assert "  job0 & job1 & job2 --> job4\n" in diagram
    assert 'job4["ci<br/>needs 3 jobs"]' in diagram
    assert '    job3["nightly<br/>only on the schedule"]' in diagram


def test_the_ci_diagram_covers_every_job_of_the_workflow(tree: Path) -> None:
    import yaml

    site_pages.build(tree)
    diagram = _generated(tree)["ci-jobs.mmd"]
    workflow = yaml.safe_load((ROOT / site_pages.CI_WORKFLOW).read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    labels = re.findall(r'^\s+job\d+\["([^"<]+)', diagram, re.MULTILINE)
    # Each job is drawn once. One that no job needs sits apart, after the others.
    assert sorted(labels) == sorted(jobs)
    assert len(labels) == len(set(labels))
    ids = dict(re.findall(r'^\s+(job\d+)\["([^"<]+)', diagram, re.MULTILINE))
    names = {name: key for key, name in ids.items()}
    for name, job in jobs.items():
        needs = job.get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        if needs:
            edge = " & ".join(names[other] for other in needs) + f" --> {names[name]}"
            assert f"  {edge}\n" in diagram, name


def test_the_runbook_draws_the_ci_diagram(site_tree: Path) -> None:
    text = (site_tree / "site" / "runbook.qmd").read_text(encoding="utf-8")
    assert "%%| file: _generated/ci-jobs.mmd" in text
    assert (site_tree / "site" / "_generated" / "ci-jobs.mmd").is_file()


GATE = """\
# Phase 3 gate: GitHub integration

- Date: 2026-11-07
- Result: **Pass on code, pending one manual step.** The smoke needs a token.
"""


def test_read_gate_takes_the_heading_date_and_bold_result(tmp_path: Path) -> None:
    path = tmp_path / "phase-3.md"
    path.write_text(GATE, encoding="utf-8")
    gate = site_pages.read_gate(path)
    assert gate == site_pages.Gate(
        3, "Phase 3 gate: GitHub integration", "2026-11-07", "Pass on code, pending one manual step"
    )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda t: t.replace("# Phase 3 gate: ", "# "), "first line must read"),
        (lambda t: t.replace("- Date: 2026-11-07\n", ""), "no '- Date:' line"),
        (lambda t: t.replace("**Pass", "Pass"), "no '- Result: **...**' line"),
    ],
    ids=["heading", "date", "result"],
)
def test_read_gate_names_the_problem(
    tmp_path: Path, edit: Callable[[str], str], message: str
) -> None:
    path = tmp_path / "phase-3.md"
    path.write_text(edit(GATE), encoding="utf-8")
    with pytest.raises(SiteError, match=re.escape(message)):
        site_pages.read_gate(path)


def test_heading_id_follows_pandoc() -> None:
    assert (
        site_pages.heading_id("Phase 5 gate: Planner, verifier and stuck handling")
        == "phase-5-gate-planner-verifier-and-stuck-handling"
    )
    assert site_pages.heading_id("3.4 Architecture decisions") == "architecture-decisions"


def test_the_gate_summary_has_a_row_per_report(tree: Path) -> None:
    site_pages.build(tree)
    summary = _generated(tree)["gates-summary.md"]
    rows = [line for line in summary.splitlines() if line.startswith("| [")]
    reports = sorted((ROOT / "docs" / "gates").glob("phase-*.md"))
    assert len(rows) == len(reports)
    tags = {
        p.number: p.tag
        for p in site_pages.read_phases((ROOT / "docs" / "progress.md").read_text(encoding="utf-8"))
    }
    for report in reports:
        text = report.read_text(encoding="utf-8")
        number = int(re.search(r"^# Phase (\d+) gate", text, re.MULTILINE).group(1))  # type: ignore[union-attr]
        result = re.search(r"^- Result: \*\*(.+?)\*\*", text, re.MULTILINE).group(1)  # type: ignore[union-attr]
        row = next(r for r in rows if r.startswith(f"| [{number}: "))
        assert f"| {tags[number]} |" in row
        assert result.rstrip(".") in row
