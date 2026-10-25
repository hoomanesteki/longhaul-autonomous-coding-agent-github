import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
PROTECT = ROOT / "scripts" / "protect_branches.sh"

REQUIRED_JOBS = {"lint", "types", "unit", "integration", "security", "compose", "ci"}


def load_workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    return data


def test_no_secrets_in_ci() -> None:
    text = WORKFLOW.read_text()
    assert "secrets." not in text
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LONGHAUL_GITHUB_TOKEN"):
        assert name not in text


def test_ci_has_required_jobs() -> None:
    jobs = load_workflow()["jobs"]
    assert set(jobs) >= REQUIRED_JOBS
    needs = jobs["ci"]["needs"]
    assert set(needs) == REQUIRED_JOBS - {"ci"}
    assert jobs["ci"]["if"] == "always()"
    for name, job in jobs.items():
        assert "timeout-minutes" in job, f"{name} has no timeout-minutes"


def test_provider_is_scripted() -> None:
    assert load_workflow()["env"]["LONGHAUL_PROVIDER"] == "scripted"


def test_triggers_and_permissions() -> None:
    wf = load_workflow()
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = wf.get("on", wf.get(True))
    assert {"pull_request", "push", "workflow_dispatch"} <= set(triggers)
    assert set(triggers["pull_request"]["branches"]) == {"dev", "main"}
    assert wf["permissions"] == {"contents": "read"}


def test_actions_use_pinned_major_versions() -> None:
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text())
    assert uses
    for ref in uses:
        assert re.search(r"@v\d+$", ref), f"{ref} is not pinned to a major version"


def test_protect_script_targets_main_and_dev() -> None:
    text = PROTECT.read_text()
    assert "set -euo pipefail" in text
    assert "main dev" in text
    assert "branches/${B}/protection" in text
    assert '"contexts": ["ci"]' in text
    assert '"strict": true' in text
    assert '"allow_force_pushes": false' in text
    assert '"allow_deletions": false' in text
