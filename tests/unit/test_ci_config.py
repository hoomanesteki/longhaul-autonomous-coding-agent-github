import re
import shutil
import subprocess
import sys
import tomllib
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


# --- mypy: strict only for the four packages the plan names -------------------------------

STRICT_MODULES = {
    "contracts.py",
    "durable/sloppy.py",
    "durable/workflows/sloppy.py",
    "context/sloppy.py",
    "cost/sloppy.py",
}
LOOSE_MODULES = {"sandbox/sloppy.py", "providers/sloppy.py", "tools/sloppy.py"}
SLOPPY_SOURCE = """\
import json


def untyped(x):
    return x


def returns_any(raw: str) -> int:
    return json.loads(raw)


def bare_generic(items: list) -> None:
    del items
"""
# What `strict` reports for SLOPPY_SOURCE: an untyped def, a returned Any, a bare generic.
STRICT_ERROR_CODES = {"no-untyped-def", "no-any-return", "type-arg"}


def test_mypy_overrides_do_not_use_the_global_strict_switch() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for override in config["tool"]["mypy"]["overrides"]:
        assert "strict" not in override, f"strict is global in mypy: {override['module']}"


def test_mypy_is_strict_only_for_the_four_packages(tmp_path: Path) -> None:
    src = tmp_path / "src" / "longhaul"
    for rel in [*STRICT_MODULES, *LOOSE_MODULES]:
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(SLOPPY_SOURCE, encoding="utf-8")
    # Every directory is a package, so mypy resolves the dotted names the overrides use.
    for directory in {p.parent for p in src.rglob("*.py")} | {src}:
        (directory / "__init__.py").touch()
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")

    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--cache-dir", str(tmp_path / ".cache")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    codes: dict[str, set[str]] = {}
    for line in result.stdout.splitlines():
        found = re.match(r"src/longhaul/(\S+\.py):\d+: error: .*\[([\w-]+)\]$", line)
        if found:
            codes.setdefault(found.group(1), set()).add(found.group(2))
    assert set(codes) == STRICT_MODULES, result.stdout
    for module, seen in codes.items():
        assert seen >= STRICT_ERROR_CODES, f"{module} was not checked strictly: {seen}"
