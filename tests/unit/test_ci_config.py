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
COMPOSE = ROOT / "docker-compose.yml"
MAKEFILE = ROOT / "Makefile"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
RUNBOOK = ROOT / "docs" / "runbook.md"

# A minimum. Later stories add jobs (console, chaos-lite), and the `ci` job has to wait for each
# of them, which test_ci_has_required_jobs checks against the real file.
REQUIRED_JOBS = {"lint", "types", "unit", "integration", "e2e", "security", "compose", "ci"}


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


# --- the test jobs that need a sandbox ------------------------------------------------------


def makefile_selection(makefile: str, target: str) -> str:
    """The -m expression that `make <target>` gives pytest, with $(OPT_IN) filled in."""
    opt_in = re.search(r"^OPT_IN\s*:=\s*(.+)$", makefile, re.M)
    assert opt_in, "the Makefile has no OPT_IN"
    recipe = re.search(rf"^{re.escape(target)}:\n\t(.+)$", makefile, re.M)
    assert recipe, f"the Makefile has no {target} target"
    found = re.search(r'-m "([^"]+)"', recipe.group(1))
    assert found, f"make {target} gives pytest no -m expression"
    return found.group(1).replace("$(OPT_IN)", opt_in.group(1).strip())


def job_selection(job: dict[str, Any]) -> str | None:
    """The -m expression of the job's pytest step, or None when it has none."""
    for step in job.get("steps", []):
        found = re.search(r'pytest\b.*?-m "([^"]+)"', " ".join(str(step.get("run", "")).split()))
        if found:
            return found.group(1)
    return None


def step_index(job: dict[str, Any], fragment: str) -> int | None:
    runs = [" ".join(str(step.get("run", "")).split()) for step in job.get("steps", [])]
    return next((i for i, run in enumerate(runs) if fragment in run), None)


@pytest.mark.parametrize(("job", "target"), [("integration", "itest"), ("e2e", "e2e")])
def test_ci_runs_the_tests_that_make_runs(job: str, target: str) -> None:
    # The comment in ci.yml says "same selection as make". Without this check the two could
    # drift: a marker added to OPT_IN would then apply on a laptop and not in CI.
    from_make = makefile_selection(MAKEFILE.read_text(encoding="utf-8"), target)
    assert job_selection(load_workflow()["jobs"][job]) == from_make


def test_the_e2e_job_runs_the_e2e_tests_only() -> None:
    selection = job_selection(load_workflow()["jobs"]["e2e"])
    assert selection is not None and selection.startswith("e2e and ")


@pytest.mark.parametrize("job", ["integration", "e2e"])
def test_jobs_with_a_sandbox_build_the_image_before_the_tests(job: str) -> None:
    # The sandbox tests fail with "image is missing" on a runner that has not built it.
    definition = load_workflow()["jobs"][job]
    build = step_index(definition, "make sandbox-image")
    tests = step_index(definition, "pytest")
    assert build is not None, f"{job} never builds the sandbox image"
    assert tests is not None, f"{job} never runs pytest"
    assert build < tests


def test_the_selection_checks_read_the_makefile_and_a_job() -> None:
    makefile = (
        "OPT_IN := not live and not slow\n\n"
        'itest:\n\tuv run pytest -m "integration and $(OPT_IN)"\n'
    )
    assert makefile_selection(makefile, "itest") == "integration and not live and not slow"
    folded = {"steps": [{"run": 'uv run pytest -q -m "integration and not live"'}]}
    assert job_selection(folded) == "integration and not live"
    assert job_selection({"steps": [{"run": "make sandbox-image"}]}) is None
    steps = {"steps": [{"run": "make sandbox-image"}, {"run": "uv run pytest"}]}
    assert (step_index(steps, "make sandbox-image"), step_index(steps, "pytest")) == (0, 1)
    assert step_index(steps, "cargo test") is None


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


# --- the Temporal server CI starts is the one the compose file pins -------------------------

# An image name with an optional tag and digest. The lookahead keeps a longer name such as
# temporalio/temporal-admin-tools from matching.
_TEMPORAL_IMAGE = re.compile(
    r"temporalio/temporal(?![\w./-])(?::(?P<tag>[^\s@\"']+))?(?:@(?P<digest>sha256:[0-9a-f]{64}))?"
)


def temporal_image_problems(workflow_text: str, compose_image: str) -> list[str]:
    """Why the Temporal image in the workflows is not the one docker-compose.yml pins."""
    wanted = _TEMPORAL_IMAGE.fullmatch(compose_image)
    assert wanted and wanted["tag"], f"docker-compose.yml must pin a tag: {compose_image}"
    refs = list(_TEMPORAL_IMAGE.finditer(workflow_text))
    if not refs:
        return ["no workflow starts the temporalio/temporal image"]
    problems: list[str] = []
    for ref in refs:
        if ref["tag"] != wanted["tag"]:
            problems.append(f"{ref[0]}: the compose file pins tag {wanted['tag']}")
        if ref["digest"] is None:
            problems.append(f"{ref[0]}: pin the image by digest as well as by tag")
        elif wanted["digest"] and ref["digest"] != wanted["digest"]:
            problems.append(f"{ref[0]}: the compose file pins a different digest")
    return problems


def test_ci_starts_the_temporal_image_that_compose_pins() -> None:
    # `make up` and the CI integration job must run the same dev server (ADR-0002). A CLI
    # downloaded without a version follows whatever Temporal released last.
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in workflow_files())
    assert temporal_image_problems(text, compose["services"]["temporal"]["image"]) == []


_DIGEST = "sha256:" + "ab" * 32


@pytest.mark.parametrize(
    ("workflow_text", "fragment"),
    [
        ("run: curl -sSf https://temporal.download/cli.sh | sh", "no workflow starts"),
        (f"image: temporalio/temporal:1.6.0@{_DIGEST}", "pins tag 1.5.1"),
        (f"image: temporalio/temporal:latest@{_DIGEST}", "pins tag 1.5.1"),
        (f"image: temporalio/temporal@{_DIGEST}", "pins tag 1.5.1"),
        ("image: temporalio/temporal:1.5.1", "by digest"),
        (f"image: temporalio/temporal:1.5.1@{_DIGEST}\nimage: temporalio/temporal:1.4.0", "1.4.0"),
    ],
    ids=["unversioned-cli", "other-tag", "latest", "no-tag", "no-digest", "second-reference"],
)
def test_temporal_image_check_notices_drift(workflow_text: str, fragment: str) -> None:
    problems = temporal_image_problems(workflow_text, "temporalio/temporal:1.5.1")
    assert any(fragment in problem for problem in problems), problems


def test_temporal_image_check_accepts_the_pinned_image() -> None:
    text = f"env:\n  TEMPORAL_IMAGE: temporalio/temporal:1.5.1@{_DIGEST}\n"
    assert temporal_image_problems(text, "temporalio/temporal:1.5.1") == []
    # The compose file may take a digest later. CI then has to use the same one.
    assert temporal_image_problems(text, f"temporalio/temporal:1.5.1@{_DIGEST}") == []
    other = f"temporalio/temporal:1.5.1@sha256:{'cd' * 32}"
    assert temporal_image_problems(text, other)


def test_temporal_image_check_ignores_longer_image_names() -> None:
    text = f"image: temporalio/temporal:1.5.1@{_DIGEST}\nimage: temporalio/temporal-admin-tools:9"
    assert temporal_image_problems(text, "temporalio/temporal:1.5.1") == []


# --- dependabot: version bumps go to dev, never straight to main ----------------------------


def test_dependabot_opens_pull_requests_against_dev() -> None:
    # main moves only through phase-gate pull requests. A bump aimed at main would skip dev's
    # CI and the gate, and leave main holding a commit that dev lacks.
    updates = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))["updates"]
    assert {update["package-ecosystem"] for update in updates} >= {"github-actions", "uv"}
    for update in updates:
        assert update.get("target-branch") == "dev", update["package-ecosystem"]


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
