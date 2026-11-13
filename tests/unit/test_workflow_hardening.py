"""Rules that hold for every workflow in .github/workflows, and for Dependabot.

tests/unit/test_ci_config.py checks the jobs of ci.yml. This file checks what every workflow
must do whatever its jobs are: the token scopes, the events that start it, the timeouts, what a
newer push cancels, and the linter, scanners and update coverage that guard the files.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"

# The only write scopes a job may hold, by workflow and job. Everything else reads, and the top
# of every workflow grants `contents: read` alone, so a new job starts read-only.
WRITE_SCOPES = {
    ("pages.yml", "build"): {"pages"},
    ("pages.yml", "deploy"): {"pages", "id-token"},
    ("release.yml", "releases"): {"contents"},
    ("codeql.yml", "analyze"): {"security-events"},
}
# The one write scope a job that pull requests start may hold. GitHub hands a pull request from
# a fork a read-only token whatever the file asks for, and CodeQL needs it for its upload on the
# pull requests of this repo.
PULL_REQUEST_WRITE_SCOPES = {"security-events"}
# Events whose runs get the base repo's secrets and a write token while they act on a pull
# request or a run that a fork can start. Nothing here needs them.
FORK_UNSAFE_EVENTS = {"pull_request_target", "workflow_run"}


def workflow_files() -> list[Path]:
    files = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
    assert {"ci.yml", "pages.yml", "release.yml", "codeql.yml"} <= {f.name for f in files}
    return files


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    assert isinstance(on, dict)
    return on


def write_scopes(permissions: Any) -> set[str]:
    assert isinstance(permissions, dict), f"permissions must name each scope: {permissions!r}"
    for scope, level in permissions.items():
        assert level in {"read", "write", "none"}, f"{scope}: {level!r}"
    return {scope for scope, level in permissions.items() if level == "write"}


# ---------- the token ----------
def test_every_workflow_is_read_only_unless_a_job_says_otherwise() -> None:
    found: dict[tuple[str, str], set[str]] = {}
    for path in workflow_files():
        workflow = load(path)
        assert workflow.get("permissions") == {"contents": "read"}, path.name
        for name, job in workflow["jobs"].items():
            if "permissions" in job and (scopes := write_scopes(job["permissions"])):
                found[(path.name, name)] = scopes
    assert found == WRITE_SCOPES


def test_the_permission_reader_refuses_the_shorthands() -> None:
    assert write_scopes({"contents": "read", "pages": "write"}) == {"pages"}
    for shorthand in ("write-all", "read-all", {"contents": "admin"}):
        with pytest.raises(AssertionError):
            write_scopes(shorthand)


def test_no_workflow_hands_a_write_token_to_a_fork() -> None:
    for path in workflow_files():
        workflow = load(path)
        on = triggers(workflow)
        assert not set(on) & FORK_UNSAFE_EVENTS, path.name
        if "pull_request" not in on:
            continue
        for name, job in workflow["jobs"].items():
            scopes = write_scopes(job.get("permissions", {}))
            assert scopes <= PULL_REQUEST_WRITE_SCOPES, f"{path.name}:{name} writes {scopes}"


def test_every_job_of_every_workflow_has_a_timeout() -> None:
    # A hung step otherwise holds a runner for six hours, GitHub's own limit.
    for path in workflow_files():
        for name, job in load(path)["jobs"].items():
            minutes = job.get("timeout-minutes")
            assert isinstance(minutes, int) and 0 < minutes <= 60, f"{path.name}:{name}"


# ---------- what a newer run cancels ----------
_EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
# The part of the expression language the concurrency keys use: names, quoted strings,
# comparisons, && and ||. Anything else, a function call included, fails the test instead of
# being read wrongly.
_ALLOWED = re.compile(r"[\w.'\s=!&|/-]*")


def test_no_checkout_keeps_the_token_in_the_git_config() -> None:
    # No job pushes with git, so no checkout needs to leave the token in .git/config, where any
    # later step of the job could read it.
    for path in workflow_files():
        for name, job in load(path)["jobs"].items():
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("actions/checkout"):
                    persist = step.get("with", {}).get("persist-credentials")
                    assert persist is False, f"{path.name}:{name} keeps the token"


def test_the_pre_commit_ruff_is_the_locked_ruff() -> None:
    # The hook and `make check` must agree, or a commit passes the hook and fails in CI.
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    (hook,) = [r for r in config["repos"] if r["repo"].endswith("/ruff-pre-commit")]
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    locked = re.search(r'name = "ruff"\nversion = "([^"]+)"', lock)
    assert locked is not None
    assert hook["rev"] == f"v{locked.group(1)}"


def evaluate(value: Any, github: dict[str, str]) -> Any:
    """The value of a workflow field for a run with this `github` context."""
    if not isinstance(value, str):
        return value

    def one(expression: str) -> Any:
        assert _ALLOWED.fullmatch(expression), expression
        python = expression.replace("&&", " and ").replace("||", " or ")
        return eval(python, {"__builtins__": {}}, {"github": SimpleNamespace(**github)})

    whole = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", value, re.S)
    if whole and "${{" not in whole.group(1):
        return one(whole.group(1))
    return _EXPRESSION.sub(lambda m: str(one(m.group(1))), value)


def run(event: str, ref: str, run_id: str, workflow: str) -> dict[str, str]:
    return {"event_name": event, "ref": ref, "run_id": run_id, "workflow": workflow}


def concurrency(workflow: dict[str, Any], context: dict[str, str]) -> tuple[str, bool]:
    settings = workflow["concurrency"]
    group = str(evaluate(settings["group"], context))
    return group, evaluate(settings.get("cancel-in-progress", False), context) is True


def test_the_evaluator_reads_the_keys_the_workflows_use() -> None:
    pr = run("pull_request", "refs/pull/7/merge", "11", "ci")
    push = run("push", "refs/heads/dev", "12", "ci")
    key = "ci-${{ github.event_name == 'pull_request' && github.ref || github.run_id }}"
    assert evaluate(key, pr) == "ci-refs/pull/7/merge"
    assert evaluate(key, push) == "ci-12"
    assert evaluate("${{ github.event_name == 'pull_request' }}", pr) is True
    assert evaluate("${{ github.event_name == 'pull_request' }}", push) is False
    assert evaluate("${{ github.workflow }}-${{ github.ref }}", push) == "ci-refs/heads/dev"
    with pytest.raises(AssertionError):
        evaluate("${{ fromJSON(github.ref) }}", push)


@pytest.mark.parametrize("path", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_a_newer_push_cancels_only_the_older_run_of_its_pull_request(path: Path) -> None:
    workflow = load(path)
    on = triggers(workflow)
    name = str(workflow["name"])
    assert "concurrency" in workflow, f"{path.name} has no concurrency group"
    if "pull_request" not in on:
        # Pages and releases: one run at a time, and a run in progress is never cut off.
        assert workflow["concurrency"].get("cancel-in-progress") is False
        return

    first = concurrency(workflow, run("pull_request", "refs/pull/7/merge", "1", name))
    second = concurrency(workflow, run("pull_request", "refs/pull/7/merge", "2", name))
    other = concurrency(workflow, run("pull_request", "refs/pull/8/merge", "3", name))
    assert first[0] == second[0] and second[1] is True
    assert other[0] != first[0]

    # A run on dev or main is never cancelled, and never waits for, or is replaced by, the next
    # one: each has a group of its own.
    events = [e for e in ("push", "schedule", "workflow_dispatch") if e in on]
    for event in events:
        for ref in ("refs/heads/dev", "refs/heads/main"):
            a = concurrency(workflow, run(event, ref, "41", name))
            b = concurrency(workflow, run(event, ref, "42", name))
            assert a[0] != b[0], f"{path.name}: two {event} runs on {ref} share a group"
            assert a[1] is False and b[1] is False


def test_two_workflows_never_share_a_concurrency_group() -> None:
    # Groups are shared across the repo, so two workflows with one key would cancel each other.
    for context in (
        run("pull_request", "refs/pull/7/merge", "1", ""),
        run("push", "refs/heads/main", "1", ""),
    ):
        groups = [
            concurrency(workflow, {**context, "workflow": str(workflow["name"])})[0]
            for workflow in map(load, workflow_files())
            if "concurrency" in workflow
        ]
        assert len(groups) == len(set(groups)), groups


# ---------- actionlint in the lint job ----------
def test_the_lint_job_runs_a_pinned_actionlint() -> None:
    job = load(WORKFLOWS / "ci.yml")["jobs"]["lint"]
    version, digest = job["env"]["ACTIONLINT_VERSION"], job["env"]["ACTIONLINT_SHA256"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert re.fullmatch(r"[0-9a-f]{64}", digest)

    runs = [str(step.get("run", "")) for step in job["steps"]]
    install = next(i for i, text in enumerate(runs) if "rhysd/actionlint/releases" in text)
    lint = next(i for i, text in enumerate(runs) if text.strip().startswith("actionlint"))
    # The download is the pinned release, and nothing runs it before its checksum is checked.
    assert "v${ACTIONLINT_VERSION}" in runs[install]
    assert "actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" in runs[install]
    assert 'echo "${ACTIONLINT_SHA256}  ${tarball}" | sha256sum -c -' in runs[install]
    assert install < lint
    # With no argument it checks every file in .github/workflows.
    assert runs[lint].strip() == "actionlint"


# ---------- the scanners ----------
def test_codeql_scans_python_and_typescript_on_every_change_and_weekly() -> None:
    workflow = load(WORKFLOWS / "codeql.yml")
    on = triggers(workflow)
    assert set(on["push"]["branches"]) == set(on["pull_request"]["branches"]) == {"dev", "main"}
    (cron,) = (entry["cron"] for entry in on["schedule"])
    assert re.fullmatch(r"\d+ \d+ \* \* [0-6]", cron), cron

    job = workflow["jobs"]["analyze"]
    assert set(job["strategy"]["matrix"]["language"]) == {"python", "javascript-typescript"}
    uses = [str(step.get("uses", "")).split("@")[0] for step in job["steps"]]
    assert uses.index("github/codeql-action/init") < uses.index("github/codeql-action/analyze")


def test_dependency_review_runs_on_pull_requests() -> None:
    workflow = load(WORKFLOWS / "dependency-review.yml")
    assert set(triggers(workflow)) == {"pull_request"}
    steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    review = [s for s in steps if str(s.get("uses", "")).startswith("actions/dependency-review")]
    assert len(review) == 1
    assert review[0]["with"]["fail-on-severity"] in {"low", "moderate", "high"}


# ---------- dependabot covers what the repo has ----------
# Ecosystems by the file that marks one. The agent's own target repos under tests/fixtures and
# eval/custom are code it works on, not dependencies of this project.
MARKERS = {
    "uv.lock": "uv",
    "package-lock.json": "npm",
    ".terraform.lock.hcl": "terraform",
    "Dockerfile": "docker",
}
NOT_OURS = {Path("tests/fixtures"), Path("eval/custom")}


def ecosystems_in(root: Path) -> set[tuple[str, str]]:
    """Each (ecosystem, directory) of the repo, in the form dependabot.yml names them."""
    found = {("github-actions", "/")}
    for folder, dirs, files in os.walk(root):
        here = Path(folder).relative_to(root)
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".") and d != "node_modules" and here / d not in NOT_OURS
        ]
        for name in files:
            if name in MARKERS:
                found.add((MARKERS[name], "/" + here.as_posix() if here.parts else "/"))
    return found


def configured(updates: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {
        (update["package-ecosystem"], directory.rstrip("/") or "/")
        for update in updates
        for directory in update.get("directories", [update.get("directory")])
    }


def test_dependabot_covers_every_ecosystem_of_the_repo() -> None:
    updates = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8"))["updates"]
    missing = ecosystems_in(ROOT) - configured(updates)
    assert missing == set()
    # The Dockerfiles, the console and the lock files: the walk must find them, or it proves
    # nothing.
    assert {"docker", "npm", "terraform", "uv"} <= {e for e, _ in ecosystems_in(ROOT)}


def test_the_ecosystem_walk_finds_a_new_folder_and_skips_the_fixtures(tmp_path: Path) -> None:
    for rel in (
        "uv.lock",
        "docker/api/Dockerfile",
        "web/package-lock.json",
        "web/node_modules/x/package-lock.json",
        "tests/fixtures/repos/calc/uv.lock",
        ".venv/lib/Dockerfile",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).touch()

    assert ecosystems_in(tmp_path) == {
        ("github-actions", "/"),
        ("uv", "/"),
        ("docker", "/docker/api"),
        ("npm", "/web"),
    }
    assert configured([{"package-ecosystem": "docker", "directories": ["/a/", "/b"]}]) == {
        ("docker", "/a"),
        ("docker", "/b"),
    }
