"""Publishing the docs site: the docs job in ci.yml, pages.yml, and the notes that explain them."""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

SITE_URL = "https://hoomanesteki.github.io/longhaul-autonomous-coding-agent-github/"
QUARTO_VERSION = "1.10.18"
# Render, then check every link, anchor and image under the Pages address (scripts/check_site.py).
SITE_COMMANDS = ["quarto render site", "make site-check"]


def workflow(name: str) -> dict[str, Any]:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def steps_using(job: dict[str, Any], action: str) -> list[dict[str, Any]]:
    """The steps of `job` that use `action`, whatever its version."""
    return [s for s in job["steps"] if str(s.get("uses", "")).split("@")[0] == action]


def run_commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_the_docs_job_renders_the_site_with_the_pinned_quarto() -> None:
    ci = workflow("ci.yml")
    job = ci["jobs"]["docs"]
    for setup in steps_using(job, "quarto-dev/quarto-actions/setup"):
        assert re.search(r"@v\d+$", setup["uses"])
        assert setup["with"]["version"] == QUARTO_VERSION
    assert run_commands(job) == SITE_COMMANDS
    assert "docs" in ci["jobs"]["ci"]["needs"]


def test_pages_publishes_on_a_push_to_main_and_by_hand_only() -> None:
    pages = workflow("pages.yml")
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = pages.get("on", pages.get(True))
    assert set(triggers) == {"push", "workflow_dispatch"}
    assert triggers["push"]["branches"] == ["main"]


def test_pages_builds_then_deploys_one_deployment_at_a_time() -> None:
    pages = workflow("pages.yml")
    assert pages["concurrency"] == {"group": "pages", "cancel-in-progress": False}
    build, deploy = pages["jobs"]["build"], pages["jobs"]["deploy"]
    assert deploy["needs"] == "build"
    assert deploy["environment"]["name"] == "github-pages"
    assert run_commands(build) == SITE_COMMANDS
    (artifact,) = steps_using(build, "actions/upload-pages-artifact")
    assert artifact["with"]["path"] == "site/_site"
    assert steps_using(build, "actions/configure-pages")
    assert steps_using(deploy, "actions/deploy-pages")


def test_pages_deploys_unless_the_owner_turns_it_off() -> None:
    # Pages is on (source GitHub Actions), so every push to main publishes the site. The
    # repository variable PAGES_ENABLED set to false stops the deploy without a code change.
    pages = workflow("pages.yml")
    gate = "vars.PAGES_ENABLED != 'false'"
    build, deploy = pages["jobs"]["build"], pages["jobs"]["deploy"]
    assert deploy["if"] == gate
    for action in ("actions/configure-pages", "actions/upload-pages-artifact"):
        (step,) = steps_using(build, action)
        assert step["if"] == gate
    # The render and the check are never gated, so a broken page or link still fails the run.
    for command in SITE_COMMANDS:
        step = [s for s in build["steps"] if s.get("run") == command]
        assert step and "if" not in step[0], command


def test_pages_checks_the_site_before_it_uploads_it() -> None:
    steps = workflow("pages.yml")["jobs"]["build"]["steps"]
    names = [str(step.get("run") or step.get("uses")).split("@")[0] for step in steps]
    position = {name: index for index, name in enumerate(names)}
    assert (
        position["quarto render site"]
        < position["make site-check"]
        < position["actions/upload-pages-artifact"]
    )


def test_only_the_pages_deploy_job_can_mint_a_token() -> None:
    # The Pages scopes belong to this workflow. The id-token scope, which lets a job ask GitHub
    # for an OIDC token, belongs to its deploy job alone. Every job of ci.yml keeps what the
    # workflow gives it, which is read access to the repository. test_workflow_hardening.py
    # lists the few jobs of other workflows that may write.
    ci = workflow("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in ci["jobs"].values())

    pages = workflow("pages.yml")
    assert pages["permissions"] == {"contents": "read"}
    assert pages["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert "id-token" not in pages["jobs"]["build"]["permissions"]


def test_ci_and_pages_render_with_the_same_quarto() -> None:
    def versions(name: str, job: str) -> set[str]:
        # A job sets Quarto up once and tries again when the first download fails.
        setups = steps_using(workflow(name)["jobs"][job], "quarto-dev/quarto-actions/setup")
        assert len(setups) == 2, setups
        assert setups[0].get("continue-on-error") is True
        assert setups[1]["if"] == f"steps.{setups[0]['id']}.outcome == 'failure'"
        return {str(setup["with"]["version"]) for setup in setups}

    assert versions("ci.yml", "docs") == versions("pages.yml", "build") == {QUARTO_VERSION}


def test_the_readme_links_the_docs_site_near_the_top() -> None:
    top = "\n".join((ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:12])
    assert SITE_URL in top


def test_the_runbook_says_how_the_site_is_published_and_stopped() -> None:
    runbook = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    assert "Build and deployment, Source" in runbook
    assert "each push to `main` publishes the site" in runbook
    assert "`PAGES_ENABLED` with the value `false`" in runbook
    assert SITE_URL in runbook
    assert QUARTO_VERSION in runbook
    assert "| `docs` |" in runbook, "the CI job table must list the docs job"
