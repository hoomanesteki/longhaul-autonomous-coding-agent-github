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

# The navbar, in order. Results is not a doc of its own, but it is where Phase 9 numbers land.
NAVBAR = [
    ("Overview", "index.qmd"),
    ("Architecture", "architecture.qmd"),
    ("Decisions", "decisions.qmd"),
    ("Progress", "progress.qmd"),
    ("Gate reports", "gates.qmd"),
    ("Results", "results.qmd"),
    ("Runbook", "runbook.qmd"),
    ("Plan", "plan.qmd"),
]


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


def test_the_navbar_lists_every_page_in_order() -> None:
    items = quarto_config()["website"]["navbar"]["left"]
    assert [(item["text"], item["href"]) for item in items] == NAVBAR
    for _, href in NAVBAR:
        assert (SITE / href).is_file(), href


def test_every_top_level_page_is_in_the_navbar() -> None:
    pages = {path.name for path in SITE.glob("*.qmd")}
    assert pages == {href for _, href in NAVBAR}


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
    # .claude/worktrees, and it renders none without saying so. The list avoids that.
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
    assert phony and {"site", "site-preview"} <= set(phony.group(1).split())
    assert re.search(r"^site:\n\tquarto render site$", makefile, re.MULTILINE)
    assert re.search(r"^site-preview:\n\tquarto preview site$", makefile, re.MULTILINE)


def test_git_ignores_rendered_output_and_generated_pages_but_not_the_sources() -> None:
    lines = {line.strip() for line in (ROOT / ".gitignore").read_text("utf-8").splitlines()}
    assert {"site/_site/", "site/.quarto/", "site/_generated/", "site/adr/"} <= lines
    # The Python template ignores a top-level site/ folder for mkdocs. That would hide the
    # Quarto project, so the entry has to stay out.
    assert not {"/site", "/site/", "site", "site/"} & lines
