"""The docs site project: site/_quarto.yml, its filter, the Makefile targets and ignore rules."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "site"

REPO_URL = "https://github.com/hoomanesteki/longhaul-autonomous-coding-agent-github"
SITE_URL = "https://hoomanesteki.github.io/longhaul-autonomous-coding-agent-github/"

# The navbar, in order: the overview, then four menus grouped by what a reader is after.
NAVBAR: list[tuple[str, list[tuple[str, str]]]] = [
    ("Overview", [("Overview", "index.qmd")]),
    (
        "How it works",
        [
            ("Architecture", "architecture.qmd"),
            ("Decisions", "decisions.qmd"),
            ("Console", "console.qmd"),
        ],
    ),
    (
        "Proof",
        [
            ("Results", "results.qmd"),
            ("Gate reports", "gates.qmd"),
            ("Case study", "case-study.qmd"),
        ],
    ),
    (
        "Operate",
        [
            ("Runbook", "runbook.qmd"),
            ("Security", "security.qmd"),
            ("Deploy to AWS", "deploy-aws.qmd"),
        ],
    ),
    ("Project", [("Plan", "plan.qmd"), ("Progress", "progress.qmd"), ("Demo script", "demo.qmd")]),
]
PAGES = [href for _, links in NAVBAR for _, href in links]


def quarto_config() -> dict[str, Any]:
    data = yaml.safe_load((SITE / "_quarto.yml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_the_project_is_a_website_rendered_into_site_underscore_site() -> None:
    config = quarto_config()
    assert config["project"]["type"] == "website"
    assert config["project"]["output-dir"] == "_site"
    assert config["website"]["title"] == "Longhaul"
    assert config["website"]["site-url"] == SITE_URL


def navbar() -> list[tuple[str, list[tuple[str, str]]]]:
    """The navbar as (label, [(text, href)]): a plain link is a group of one."""
    found = []
    for item in quarto_config()["website"]["navbar"]["left"]:
        if "menu" in item:
            links = [(entry["text"], entry["href"]) for entry in item["menu"]]
        else:
            links = [(item["text"], item["href"])]
        found.append((item["text"], links))
    return found


def test_the_navbar_lists_every_page_in_order() -> None:
    assert navbar() == NAVBAR
    for href in PAGES:
        assert (SITE / href).is_file(), href
    assert len(PAGES) == len(set(PAGES)), "a page sits in two menus"


def test_every_top_level_page_is_in_the_navbar() -> None:
    # 404.qmd is the page GitHub Pages shows for an address it does not have. It is no page to
    # go to, so it stays out of the navbar.
    pages = {path.name for path in SITE.glob("*.qmd")}
    assert pages == {*PAGES, "404.qmd"}


def test_the_not_found_page_leads_back_into_the_site() -> None:
    text = (SITE / "404.qmd").read_text(encoding="utf-8")
    assert "](index.qmd)" in text
    assert "{{< meta source-repo >}}" in text


def front_matter(page: Path) -> dict[str, Any]:
    data = yaml.safe_load(page.read_text(encoding="utf-8").split("---\n")[1])
    assert isinstance(data, dict)
    return data


@pytest.mark.parametrize("page", sorted(path.name for path in SITE.glob("*.qmd")))
def test_every_page_sets_its_tab_and_card_title(page: str) -> None:
    # Quarto joins the title and the site name with an en dash in the social card unless the
    # page sets pagetitle. The ADR pages get theirs from scripts/site_pages.py.
    meta = front_matter(SITE / page)
    expected = "Longhaul" if meta["title"] == "Longhaul" else f"{meta['title']} | Longhaul"
    assert meta["pagetitle"] == expected
    if page != "404.qmd":
        assert meta["description"].endswith("."), "search engines and the card show it"


def test_the_site_has_a_social_card_and_a_favicon() -> None:
    website = quarto_config()["website"]
    assert website["open-graph"] is True
    assert website["twitter-card"] is True
    assert website["favicon"] == "favicon.svg"
    card = SITE / website["image"]
    # A PNG of 1200 by 630, the size the cards are drawn at.
    header = card.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n"
    assert (int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")) == (
        1200,
        630,
    )
    assert website["image-alt"]
    # The home page shows screenshots, and Quarto would take the first one for its card.
    assert front_matter(SITE / "index.qmd")["image"] == website["image"]


def test_the_pages_read_markdown_as_github_does() -> None:
    # Smart typography would turn "--" into an en dash, which the voice guide bans, and the gate
    # reports hold many command options such as --window. GitHub also starts a list right after
    # a line of text, and the plan has many lists that follow a line ending in a colon.
    reader = "markdown-smart+lists_without_preceding_blankline"
    assert quarto_config()["format"]["html"]["from"] == reader
    fragments = (SITE / "filters" / "fragments.lua").read_text(encoding="utf-8")
    assert fragments.count(f'"{reader}"') == 2
    assert 'pandoc.read(read(relative), "markdown")' not in fragments


def test_the_theme_keeps_code_and_scrolling_boxes_readable() -> None:
    html = quarto_config()["format"]["html"]
    assert html["highlight-style"] == "a11y"
    script = SITE / "_scroll-regions.html"
    assert script.name in html["include-after-body"]
    assert 'setAttribute("tabindex", "0")' in script.read_text(encoding="utf-8")
    # axe-core found light text on a light gray code block in dark mode, white text on the
    # light blue button, and orange tokens just under 4.5 on the light code block.
    dark = (SITE / "theme-dark.scss").read_text(encoding="utf-8")
    assert "$code-block-bg:" in dark
    assert "$min-contrast-ratio: 4.5;" in dark
    assert "$code-block-bg:" in (SITE / "theme-light.scss").read_text(encoding="utf-8")


def test_the_navbar_marks_the_current_page_inside_its_menus() -> None:
    # Quarto marks only a top-level link as the current page, so a page in a menu showed none.
    html = quarto_config()["format"]["html"]
    assert "_nav-current.html" in html["include-after-body"]
    script = (SITE / "_nav-current.html").read_text(encoding="utf-8")
    assert ".dropdown-item" in script
    assert 'setAttribute("aria-current", "page")' in script
    assert 'toggle.classList.add("active")' in script
    # An ADR page has no menu item of its own and belongs to Decisions.
    assert '"/adr/"' in script
    assert '"/decisions.html"' in script
    assert ("Decisions", "decisions.qmd") in NAVBAR[1][1]
    # The marked item takes the color of the current top-level link, not Bootstrap's filled blue.
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    assert ".dropdown-item.active" in css
    assert "var(--bs-navbar-active-color)" in css


def test_the_wide_diagrams_fit_a_desktop_page_and_show_their_scroll_bar() -> None:
    # A min-width of 1300px cut the system context diagram at a 1280px screen, where its frame
    # is 1229px wide, and macOS hid the scroll bar that would have said so.
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    wide = css.index("@media (min-width: 1200px)")
    assert "figure.quarto-float-fig svg.mermaid-js {\n    min-width: 0;" in css[wide:]
    assert ".mermaid-frame::-webkit-scrollbar {" in css
    assert "figure.quarto-float-fig > div::-webkit-scrollbar-thumb" in css
    assert "scrollbar-color:" in css


def test_the_overview_links_every_page_of_the_navbar() -> None:
    # The "Where to go" cards of the home page are the map of the site.
    overview = (SITE / "index.qmd").read_text(encoding="utf-8")
    for href in PAGES:
        if href != "index.qmd":
            assert f"]({href})" in overview, href


def test_the_cards_tiles_and_strip_have_colors_for_both_themes() -> None:
    # Quarto compiles the light and the dark theme apart, so Bootstrap's own tertiary colors
    # would be the light ones in both. styles.css sets each color the cards use per theme.
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    tokens = ("--lh-card-bg:", "--lh-card-border:", "--lh-muted:", "--lh-accent:", "--lh-track:")
    for theme in ("body.quarto-light {", "body.quarto-dark {"):
        block = css[css.index(theme) : css.index("}", css.index(theme))]
        for token in tokens:
            assert token in block, (theme, token)
    for selector in (".answer-card", ".page-card", ".stat-tile", ".phase-bar > span"):
        assert selector in css, selector
    # The whole card is the link, so a keyboard user needs a ring around it.
    assert ".page-card:focus-within" in css


def test_the_footer_and_the_navbar_link_to_the_repository() -> None:
    config = quarto_config()
    assert REPO_URL in config["website"]["page-footer"]["center"]
    tools = config["website"]["navbar"]["tools"]
    assert [tool["href"] for tool in tools] == [REPO_URL]
    # The "rendered from" links read this key, so it must be the same repository.
    assert config["source-repo"] == REPO_URL


def test_the_theme_has_a_light_and_a_dark_variant() -> None:
    theme = quarto_config()["format"]["html"]["theme"]
    assert set(theme) == {"light", "dark"}
    for variant in theme.values():
        assert "cosmo" in variant
    for name in ("theme-light.scss", "theme-dark.scss", "styles.css", "favicon.svg"):
        assert (SITE / name).is_file(), name


def test_quarto_runs_the_script_and_renders_the_pages_it_writes() -> None:
    project = quarto_config()["project"]
    command = project["pre-render"].split()
    assert command[0] == "python3"
    # Quarto runs the script from site/, so the path is relative to that folder.
    assert (SITE / command[1]).resolve() == ROOT / "scripts" / "site_pages.py"
    # Quarto finds no pages by itself when a checkout sits under a dot folder such as
    # .worktrees, and it renders none without saying so. The list avoids that.
    assert project["render"] == ["*.qmd", "adr/*.qmd"]


def test_the_filters_are_listed_in_order_and_exist() -> None:
    # fragments.lua goes first, so docs.lua also sees the generated files it embeds.
    filters = quarto_config()["filters"]
    assert filters == ["filters/fragments.lua", "filters/docs.lua"]
    for name in filters:
        assert (SITE / name).is_file(), name


def test_the_tab_title_is_set_by_the_filter_so_quarto_adds_no_en_dash() -> None:
    # Quarto joins the page title and the site name with an en dash, which the voice guide
    # bans. The prefix is blanked, and docs.lua writes the whole title.
    assert quarto_config()["format"]["html"]["title-prefix"] == ""
    assert "pagetitle" in (SITE / "filters" / "docs.lua").read_text(encoding="utf-8")


def test_makefile_has_the_site_targets() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    phony = re.search(r"^\.PHONY:(.*)$", makefile, re.MULTILINE)
    assert phony and {"site", "site-preview", "site-check", "site-links"} <= set(
        phony.group(1).split()
    )
    assert re.search(r"^site:\n\tquarto render site$", makefile, re.MULTILINE)
    assert re.search(r"^site-preview:\n\tquarto preview site$", makefile, re.MULTILINE)
    assert re.search(r"^site-check:\n\t\$\(SITE_CHECK\)$", makefile, re.MULTILINE)
    # External links need the network, so only the target run on request asks them.
    assert re.search(r"^site-links:\n\t\$\(SITE_CHECK\) --external$", makefile, re.MULTILINE)


def test_the_site_check_reads_the_site_under_the_address_pages_gives_it() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    command = re.search(r"^SITE_CHECK := (.*)$", makefile, re.MULTILINE)
    url = re.search(r"^SITE_URL := (.*)$", makefile, re.MULTILINE)
    assert command and url
    assert url.group(1) == quarto_config()["website"]["site-url"] == SITE_URL
    words = command.group(1).split()
    assert words[:3] == ["python3", "scripts/check_site.py", "site/_site"]
    prefix = words[words.index("--prefix") + 1]
    assert SITE_URL.endswith(prefix.lstrip("/"))
    assert prefix == "/longhaul-autonomous-coding-agent-github/"
    assert words[words.index("--site-url") + 1] == "$(SITE_URL)"


def test_git_ignores_rendered_output_and_generated_pages_but_not_the_sources() -> None:
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text("utf-8").splitlines()}
    assert {"site/_site/", "site/.quarto/", "site/_generated/", "site/adr/"} <= lines
    # The Python template ignores a top-level site/ folder for mkdocs. That would hide the
    # Quarto project, so the entry has to stay out.
    assert not {"/site", "/site/", "site", "site/"} & lines
