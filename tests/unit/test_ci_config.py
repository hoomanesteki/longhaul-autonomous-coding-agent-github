import json
import os
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
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "ci.yml"
PROTECT = ROOT / "scripts" / "protect_branches.sh"
RUNBOOK = ROOT / "docs" / "runbook.md"

# A minimum. Later stories add jobs (e2e, console, chaos-lite), and the `ci` job has to wait
# for each of them, which test_ci_waits_for_every_other_job checks against the real file.
REQUIRED_JOBS = {"lint", "types", "unit", "integration", "security", "compose", "ci"}


def load_workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text())
    assert isinstance(data, dict)
    return data


def workflow_files() -> list[Path]:
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert WORKFLOW in files, "the glob must find the main workflow, or the scans prove nothing"
    return files


# --- secrets: CI has none (invariant I8) ---------------------------------------------------

_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_SECRETS_WORD = re.compile(r"\bsecrets\b")
_SECRETS_ACCESS = re.compile(r"\bsecrets(?:\.[A-Za-z_*]|\s*\[)")
_SECRETS_KEY = re.compile(r"^\s*(?:-\s*)?secrets\s*:", re.M)


def secret_references(text: str) -> list[str]:
    """Every way a workflow can read the repository's secrets, as the matching snippets.

    That is the `secrets` context in any expression (secrets.X, secrets['X'] and
    toJSON(secrets)), and the `secrets:` key that hands them to a reusable workflow.
    """
    found = [m.group(0) for m in _EXPRESSION.finditer(text) if _SECRETS_WORD.search(m.group(1))]
    found += [m.group(0) for m in _SECRETS_ACCESS.finditer(text)]
    found += [m.group(0).strip() for m in _SECRETS_KEY.finditer(text)]
    return found


def test_no_secrets_in_ci() -> None:
    # The name is the one LH-003 and the phase 0 gate report cite. It now scans every workflow
    # file and every way of reading a secret, not only `secrets.` in ci.yml.
    for path in workflow_files():
        text = path.read_text(encoding="utf-8")
        assert not secret_references(text), f"{path.name} reads secrets"
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "LONGHAUL_GITHUB_TOKEN"):
            assert name not in text, f"{path.name} mentions {name}"


@pytest.mark.parametrize(
    "snippet",
    [
        "env:\n  KEY: ${{ secrets.CLAUDE_KEY }}",
        "env:\n  KEY: ${{secrets.CLAUDE_KEY}}",
        "env:\n  KEY: ${{ secrets['CLAUDE_KEY'] }}",
        'env:\n  KEY: ${{ secrets["CLAUDE_KEY"] }}',
        "env:\n  ALL: ${{ toJSON(secrets) }}",
        "with:\n  token: ${{ format('{0}', secrets.PAT) }}",
        "if: ${{ github.event_name == 'push' && secrets.X != '' }}",
        "jobs:\n  call:\n    uses: ./.github/workflows/other.yml\n    secrets: inherit",
        "jobs:\n  call:\n    secrets:\n      token: abc",
        "      - run: echo ${{ secrets.TOKEN }}",
    ],
    ids=[
        "dot",
        "dot-no-spaces",
        "single-quoted-index",
        "double-quoted-index",
        "to-json",
        "inside-format",
        "inside-if",
        "inherit",
        "secrets-map",
        "inside-run",
    ],
)
def test_secret_scan_catches_every_way_to_read_secrets(snippet: str) -> None:
    assert secret_references(snippet)


@pytest.mark.parametrize(
    "snippet",
    [
        "      - name: Scan git history for secrets\n        run: gitleaks detect",
        "# CI has no secrets.",
        "env:\n  TOKEN: ${{ github.token }}",
        "env:\n  RESULTS: ${{ join(needs.*.result, ' ') }}",
    ],
    ids=["step-name", "comment", "github-token", "needs-results"],
)
def test_secret_scan_leaves_other_text_alone(snippet: str) -> None:
    assert secret_references(snippet) == []


# --- jobs ----------------------------------------------------------------------------------


def needs_of(job: dict[str, Any]) -> set[str]:
    needs = job.get("needs", [])
    return {needs} if isinstance(needs, str) else set(needs)


def jobs_the_aggregate_ignores(workflow: dict[str, Any]) -> set[str]:
    """Jobs whose failure would not stop `ci`, and so would not stop a merge."""
    jobs = workflow["jobs"]
    return set(jobs) - {"ci"} - needs_of(jobs["ci"])


def test_ci_has_required_jobs() -> None:
    workflow = load_workflow()
    jobs = workflow["jobs"]
    assert set(jobs) >= REQUIRED_JOBS
    # Branch protection requires only `ci`. It has to wait for every other job, including the
    # ones later stories add, which a fixed list of names would not notice.
    assert jobs_the_aggregate_ignores(workflow) == set()
    assert needs_of(jobs["ci"]) <= set(jobs) - {"ci"}, "ci waits for a job that does not exist"
    assert jobs["ci"]["if"] == "always()"
    for name, job in jobs.items():
        assert "timeout-minutes" in job, f"{name} has no timeout-minutes"


def test_aggregate_check_notices_a_job_it_does_not_wait_for() -> None:
    workflow = {
        "jobs": {
            "lint": {},
            "e2e": {"steps": [{"run": "exit 1"}]},
            "ci": {"needs": ["lint"]},
        }
    }
    assert jobs_the_aggregate_ignores(workflow) == {"e2e"}
    workflow["jobs"]["ci"]["needs"] = "lint"
    assert jobs_the_aggregate_ignores(workflow) == {"e2e"}
    workflow["jobs"]["ci"]["needs"] = ["lint", "e2e"]
    assert jobs_the_aggregate_ignores(workflow) == set()


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
    uses = [
        ref
        for path in workflow_files()
        for ref in re.findall(r"uses:\s*(\S+)", path.read_text(encoding="utf-8"))
    ]
    assert uses
    for ref in uses:
        assert re.search(r"@v\d+$", ref), f"{ref} is not pinned to a major version"


# --- branch protection ---------------------------------------------------------------------

REQUIRED_FIELDS = {
    "required_status_checks",
    "enforce_admins",
    "required_pull_request_reviews",
    "restrictions",
}


def protection_payload(script: str) -> dict[str, Any]:
    found = re.search(r"<<'JSON'\n(.*?)\nJSON\n", script, re.S)
    assert found, "protect_branches.sh has no JSON heredoc"
    payload = json.loads(found.group(1))
    assert isinstance(payload, dict)
    return payload


def payload_problems(payload: dict[str, Any]) -> list[str]:
    """Why GitHub would reject this body, or why it would not protect what the plan says."""
    problems = [f"missing field {name}" for name in sorted(REQUIRED_FIELDS - payload.keys())]
    checks = payload.get("required_status_checks")
    if not isinstance(checks, dict) or checks.get("contexts") != ["ci"]:
        problems.append("the only required check must be ci")
    if not isinstance(checks, dict) or checks.get("strict") is not True:
        problems.append("the check must be strict, so a branch has to be up to date")
    reviews = payload.get("required_pull_request_reviews")
    if not isinstance(reviews, dict) or reviews.get("required_approving_review_count") != 0:
        problems.append("a pull request is required, with 0 approvals")
    if payload.get("restrictions") is not None:
        problems.append("restrictions must be null: anyone may push through a pull request")
    for name in ("allow_force_pushes", "allow_deletions"):
        if payload.get(name) is not False:
            problems.append(f"{name} must be false")
    return problems


def test_protect_script_targets_main_and_dev() -> None:
    text = PROTECT.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    assert re.search(r"^for B in main dev; do$", text, re.M)
    assert "branches/${B}/protection" in text
    assert payload_problems(protection_payload(text)) == []


def test_admins_are_deliberately_not_enforced() -> None:
    # The owner merges gate pull requests and the gatekeeper pushes dev as the admin, so the
    # rules bind collaborators and bots but not admins. The value stays false only while the
    # script and the runbook both say so, because LH-003 reads as if every push is rejected.
    script = PROTECT.read_text(encoding="utf-8")
    assert protection_payload(script)["enforce_admins"] is False
    comments = "\n".join(line for line in script.splitlines() if line.lstrip().startswith("#"))
    assert "enforce_admins" in comments, "protect_branches.sh must explain the false value"
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert "`enforce_admins` is false on purpose" in runbook


@pytest.mark.parametrize(
    "dropped",
    ["enforce_admins", "required_pull_request_reviews", "restrictions", "required_status_checks"],
)
def test_payload_check_notices_a_missing_required_field(dropped: str) -> None:
    payload = protection_payload(PROTECT.read_text(encoding="utf-8"))
    del payload[dropped]
    assert f"missing field {dropped}" in payload_problems(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("required_status_checks", {"strict": True, "contexts": []}),
        ("required_status_checks", {"strict": False, "contexts": ["ci"]}),
        ("required_pull_request_reviews", None),
        ("restrictions", {"users": [], "teams": []}),
        ("allow_force_pushes", True),
        ("allow_deletions", True),
    ],
)
def test_payload_check_notices_a_weakened_rule(field: str, value: Any) -> None:
    payload = protection_payload(PROTECT.read_text(encoding="utf-8"))
    payload[field] = value
    assert payload_problems(payload)


FAKE_GH = """\
#!/bin/sh
# Records the arguments and the request body of each call instead of reaching GitHub.
printf 'ARGS %s\\n' "$*" >> "$GH_LOG"
cat >> "$GH_LOG"
printf '\\nEND\\n' >> "$GH_LOG"
"""


def test_protect_script_sends_the_payload_for_main_then_dev(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GH_LOG": str(log)}

    result = subprocess.run(
        ["bash", str(PROTECT), "octo/longhaul"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = re.findall(
        r"^ARGS ([^\n]*)\n(.*?)\nEND\n", log.read_text(encoding="utf-8"), re.S | re.M
    )
    assert [args for args, _ in calls] == [
        f"api -X PUT repos/octo/longhaul/branches/{branch}/protection --input -"
        for branch in ("main", "dev")
    ]
    expected = protection_payload(PROTECT.read_text(encoding="utf-8"))
    for _, body in calls:
        assert json.loads(body) == expected
    assert payload_problems(expected) == []


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
