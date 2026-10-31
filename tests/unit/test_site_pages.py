"""scripts/site_pages.py: the pre-render step that turns docs/ into pages for the Quarto site."""

import ast
import importlib.util
import re
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

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


def _record(number: str, title: str = "T") -> site_pages.Adr:
    return site_pages.Adr(number, f"ADR-{number}: {title}", title, "Accepted", "d", f"{number}-t")


def test_adr_page_title_carries_a_colon_and_quotes_safely() -> None:
    adr = site_pages.Adr("0004", 'ADR-0004: A "quoted": title', "A", "Accepted", "d", "0004-x")
    page = site_pages.adr_page(adr, "../../docs/adr/0004-x.md", None, None)
    assert page.splitlines()[1] == 'title: "ADR-0004: A \\"quoted\\": title"'


def test_adr_page_includes_the_doc_and_drops_its_own_heading() -> None:
    page = site_pages.adr_page(_record("0004"), "../../docs/adr/0004-t.md", None, None)
    assert "::: {.drop-title}\n{{< include ../../docs/adr/0004-t.md >}}\n:::" in page
    assert "{{< meta source-repo >}}/blob/main/docs/adr/0004-t.md" in page


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
    (tmp_path / "site").mkdir()
    return tmp_path


def _generated(tree: Path) -> dict[str, str]:
    folder = tree / "site" / "_generated"
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir())}


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
    assert "| [0099](adr/0099-use-a-queue.qmd) | Use a queue | Accepted | 2026-10-30 |" in index
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
    (tree / "docs" / "gates" / "phase-98.md").write_text("# Phase 98 gate\n", encoding="utf-8")
    site_pages.build(tree)
    assert _generated(tree)["gates.txt"].splitlines() == [*before, "../docs/gates/phase-98.md"]


def test_the_status_table_matches_the_progress_file(tree: Path) -> None:
    site_pages.build(tree)
    progress = (ROOT / "docs" / "progress.md").read_text(encoding="utf-8")
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in progress.splitlines()
        if line.startswith("| LH-")
    ]
    done = sum(1 for row in rows if row[3] == "Done")
    status = _generated(tree)["status.md"]
    assert status.startswith(f"{done} of {len(rows)} stories are done")
    phases = len(re.findall(r"^## Phase \d", progress, re.MULTILINE))
    table = [line for line in status.splitlines() if line.startswith("| ")]
    assert len(table) == phases + 1


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
        # Both helpers fail on the first path that points at nothing. Every page uses one.
        assert _pulled_in(page) or _embedded(page, site), page.name


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
    included = {
        "progress.qmd": docs / "progress.md",
        "runbook.qmd": docs / "runbook.md",
        "architecture.qmd": docs / "architecture.md",
        "plan.qmd": docs / "plan" / "longhaul-build-plan.md",
    }
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
