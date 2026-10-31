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
    (setup,) = steps_using(job, "quarto-dev/quarto-actions/setup")
    assert re.search(r"@v\d+$", setup["uses"])
    assert setup["with"]["version"] == QUARTO_VERSION
    assert run_commands(job) == ["quarto render site"]
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
    assert run_commands(build) == ["quarto render site"]
    (artifact,) = steps_using(build, "actions/upload-pages-artifact")
    assert artifact["with"]["path"] == "site/_site"
    assert steps_using(build, "actions/configure-pages")
    assert steps_using(deploy, "actions/deploy-pages")


def test_only_the_pages_deploy_job_can_mint_a_token() -> None:
    # The write scopes belong to one workflow. The id-token scope, which lets a job ask GitHub
    # for an OIDC token, belongs to its deploy job alone. Every other job keeps what the
    # workflow gives it, which is read access to the repository.
    ci = workflow("ci.yml")
    assert ci["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in ci["jobs"].values())

    pages = workflow("pages.yml")
    assert pages["permissions"] == {"contents": "read"}
    assert pages["jobs"]["deploy"]["permissions"] == {"pages": "write", "id-token": "write"}
    assert "id-token" not in pages["jobs"]["build"]["permissions"]


def test_ci_and_pages_render_with_the_same_quarto() -> None:
    def version(name: str, job: str) -> str:
        (setup,) = steps_using(workflow(name)["jobs"][job], "quarto-dev/quarto-actions/setup")
        return str(setup["with"]["version"])

    assert version("ci.yml", "docs") == version("pages.yml", "build") == QUARTO_VERSION


def test_the_readme_links_the_docs_site_near_the_top() -> None:
    top = "\n".join((ROOT / "README.md").read_text(encoding="utf-8").splitlines()[:12])
    assert SITE_URL in top


def test_the_runbook_tells_the_owner_how_the_site_goes_live() -> None:
    runbook = (ROOT / "docs" / "runbook.md").read_text(encoding="utf-8")
    assert "Source, GitHub Actions" in runbook
    assert "first push to `main`" in runbook
    assert SITE_URL in runbook
    assert QUARTO_VERSION in runbook
    assert "| `docs` |" in runbook, "the CI job table must list the docs job"
