import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]

# Banned phrases from the voice guide (plan section 5.10). "em dashes" and "emoji"
# are checked separately: dashes by test_no_long_dashes, emoji by character.
BANNED_PHRASES = [
    "robust",
    "seamless",
    "seamlessly",
    "leverage",
    "delve",
    "comprehensive",
    "cutting-edge",
    "state-of-the-art",
    "game-changer",
    "unlock",
    "empower",
    "elevate",
    "harness the power",
    "supercharge",
    "in today's fast-paced",
    "it's worth noting",
    "furthermore",
    "moreover",
    "plethora",
    "a testament to",
    "navigate the complexities",
    "best-in-class",
]

LONG_DASHES = ("\u2014", "\u2013")  # em dash, en dash


def _site_pages(root: Path) -> list[Path]:
    """The Quarto pages under site/. Rendered output and Quarto's cache are not source."""
    site = root / "site"
    return sorted(
        path
        for path in site.rglob("*.qmd")
        if not {"_site", ".quarto"} & set(path.relative_to(site).parts)
    )


def _doc_files(root: Path) -> list[Path]:
    """README.md, every Markdown file under docs/ (the plan included) and the site pages."""
    return [root / "README.md", *sorted((root / "docs").rglob("*.md")), *_site_pages(root)]


def _phrase_files(root: Path) -> list[Path]:
    """The files the banned-phrase check reads.

    The voice guide and the plan are left out because they list the phrases. LH-004 allows
    that for this check only.
    """
    style_guide, plan_dir = root / "docs" / "style.md", root / "docs" / "plan"
    return [p for p in _doc_files(root) if p != style_guide and plan_dir not in p.parents]


def _rel(path: Path, root: Path = ROOT) -> str:
    return path.relative_to(root).as_posix()


def banned_phrase_hits(root: Path = ROOT) -> list[str]:
    patterns = {
        phrase: re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)
        for phrase in BANNED_PHRASES
    }
    hits: list[str] = []
    for path in _phrase_files(root):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for phrase, pattern in patterns.items():
                if pattern.search(line):
                    hits.append(f"{_rel(path, root)}:{lineno}: {phrase}")
    return hits


def long_dash_hits(root: Path = ROOT) -> list[str]:
    hits: list[str] = []
    for path in _doc_files(root):
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(dash in line for dash in LONG_DASHES):
                hits.append(f"{_rel(path, root)}:{lineno}")
    return hits


def test_no_banned_phrases() -> None:
    hits = banned_phrase_hits()
    assert not hits, "banned phrases found:\n" + "\n".join(hits)


def test_no_long_dashes() -> None:
    hits = long_dash_hits()
    assert not hits, "em or en dashes found:\n" + "\n".join(hits)


# --- the checks themselves, run on small trees ----------------------------------------------


def _write_tree(root: Path, rel: str, text: str) -> None:
    """A tree with an empty README.md and one doc. `rel` may be README.md itself."""
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("", encoding="utf-8")
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


DOC_PATHS = [
    "README.md",
    "docs/runbook.md",
    "docs/adr/0001-record-decisions.md",
    "docs/gates/phase-1.md",
    "docs/style.md",
    "docs/plan/longhaul-build-plan.md",
    "site/index.qmd",
    "site/adr/0001-record-decisions.qmd",
]


@pytest.mark.parametrize("rel", DOC_PATHS)
@pytest.mark.parametrize("dash", LONG_DASHES, ids=["em-dash", "en-dash"])
def test_long_dash_check_reads_every_doc_including_the_plan(
    tmp_path: Path, rel: str, dash: str
) -> None:
    # LH-004 leaves docs/style.md and docs/plan/ out of the banned-phrase check only. The plan
    # is the document edited most, so a dash there must still be found.
    _write_tree(tmp_path, rel, f"one\ntwo {dash} three\n")
    assert long_dash_hits(tmp_path) == [f"{rel}:2"]


def test_long_dash_check_passes_clean_docs(tmp_path: Path) -> None:
    _write_tree(tmp_path, "docs/plan/longhaul-build-plan.md", "Plain - hyphens, and -- too.\n")
    assert long_dash_hits(tmp_path) == []


@pytest.mark.parametrize(
    ("rel", "found"),
    [
        ("README.md", True),
        ("docs/runbook.md", True),
        ("docs/gates/phase-1.md", True),
        ("site/index.qmd", True),
        ("site/adr/0001-record-decisions.qmd", True),
        ("docs/style.md", False),
        ("docs/plan/longhaul-build-plan.md", False),
    ],
)
def test_banned_phrase_check_skips_only_the_style_guide_and_the_plan(
    tmp_path: Path, rel: str, found: bool
) -> None:
    _write_tree(tmp_path, rel, "We leverage this.\n")
    assert bool(banned_phrase_hits(tmp_path)) is found


@pytest.mark.parametrize("rel", ["site/_site/index.qmd", "site/.quarto/idx/index.qmd"])
def test_site_checks_skip_rendered_output_and_the_quarto_cache(tmp_path: Path, rel: str) -> None:
    # Neither folder holds source. A dash or a banned phrase there would be noise the author
    # cannot fix, so the checks must not report it.
    _write_tree(tmp_path, rel, "We leverage this — and this.\n")
    assert banned_phrase_hits(tmp_path) == []
    assert long_dash_hits(tmp_path) == []


def test_site_checks_ignore_files_that_are_not_pages(tmp_path: Path) -> None:
    # Fragments under site/_generated/ come from docs/, which the checks already read.
    _write_tree(tmp_path, "site/_generated/status.md", "We leverage this — and this.\n")
    assert banned_phrase_hits(tmp_path) == []
    assert long_dash_hits(tmp_path) == []
