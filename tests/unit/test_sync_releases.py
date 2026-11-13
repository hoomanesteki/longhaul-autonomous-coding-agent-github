"""The release automation: scripts/sync_releases.py and .github/workflows/release.yml."""

from __future__ import annotations

import fnmatch
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sync_releases.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sync_releases", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up by name while the class is built.
    sys.modules["sync_releases"] = module
    spec.loader.exec_module(module)
    return module


sync = _load()


def tags(*names: str) -> list[Any]:
    return [sync.Tag(name=name, message=f"{name}: notes") for name in names]


# ---------- the plan ----------
def test_every_tag_without_a_release_gets_one_oldest_first() -> None:
    plan = sync.plan_releases(tags("v1.0.0", "v0.0.1", "v0.10.0", "v0.9.0"), existing=[])

    assert [r.tag for r in plan] == ["v0.0.1", "v0.9.0", "v0.10.0", "v1.0.0"]
    assert all(r.title == r.tag and r.notes == f"{r.tag}: notes" for r in plan)
    # The newest is created last as well, so the list of releases on GitHub reads in order.
    assert [r.tag for r in plan if r.latest] == ["v1.0.0"]
    assert not any(r.prerelease for r in plan)


def test_a_second_run_creates_nothing() -> None:
    all_tags = tags("v0.0.1", "v0.1.0", "v1.0.0")
    first = sync.plan_releases(all_tags, existing=[])

    assert sync.plan_releases(all_tags, existing=[r.tag for r in first]) == []


def test_only_the_missing_tags_are_created() -> None:
    plan = sync.plan_releases(tags("v0.9.0", "v1.0.0", "v1.0.1"), existing=["v0.9.0", "v1.0.0"])

    assert [(r.tag, r.latest) for r in plan] == [("v1.0.1", True)]


def test_a_tag_for_an_older_line_never_becomes_the_latest() -> None:
    plan = sync.plan_releases(tags("v0.8.1", "v1.0.0"), existing=["v1.0.0"])

    assert [(r.tag, r.latest) for r in plan] == [("v0.8.1", False)]


def test_a_pre_release_is_marked_and_never_the_latest() -> None:
    plan = sync.plan_releases(tags("v1.0.0", "v1.1.0-rc.1"), existing=["v1.0.0"])

    assert [(r.tag, r.prerelease, r.latest) for r in plan] == [("v1.1.0-rc.1", True, False)]
    only_pre = sync.plan_releases(tags("v2.0.0-rc.1"), existing=[])
    assert [(r.prerelease, r.latest) for r in only_pre] == [(True, False)]


def test_tags_that_are_not_versions_are_left_alone() -> None:
    plan = sync.plan_releases(tags("v1", "v1.0", "nightly", "1.0.0", "v1.0.0.1"), existing=[])

    assert plan == []


def test_a_tag_without_a_message_gets_its_name_as_the_notes() -> None:
    (release,) = sync.plan_releases([sync.Tag(name="v0.1.0", message="")], existing=[])

    assert release.notes == "v0.1.0"


@pytest.mark.parametrize(
    ("lower", "higher"),
    [
        ("v0.9.0", "v0.10.0"),
        ("v0.10.0", "v1.0.0"),
        ("v1.0.0-rc.1", "v1.0.0"),
        ("v1.0.0-rc.2", "v1.0.0-rc.10"),
        ("v1.0.0-alpha", "v1.0.0-beta"),
        ("v1.0.0-rc.1", "v1.0.0-rc.a"),
    ],
)
def test_versions_sort_by_number_not_by_text(lower: str, higher: str) -> None:
    assert sync.version_key(lower) < sync.version_key(higher)


def test_the_tag_filter_of_the_workflow_matches_the_versions_the_script_reads() -> None:
    (pattern,) = triggers(workflow())["push"]["tags"]
    for name in ("v0.0.1", "v1.0.0", "v1.1.0-rc.1"):
        assert fnmatch.fnmatch(name, pattern) and sync.version_key(name) is not None


# ---------- reading the tags ----------
def test_parse_tags_reads_subject_and_body_of_each_tag() -> None:
    output = (
        "\0v0.0.1\0Phase 0: foundation\0\n"
        "\0v1.0.0\0v1.0.0: release\0First line.\n\n- a list\n- with items\n\n"
    )

    assert sync.parse_tags(output) == [
        sync.Tag(name="v0.0.1", message="Phase 0: foundation"),
        sync.Tag(name="v1.0.0", message="v1.0.0: release\n\nFirst line.\n\n- a list\n- with items"),
    ]
    assert sync.parse_tags("") == []
    with pytest.raises(ValueError):
        sync.parse_tags("\0v0.0.1\0subject only\n")


def test_the_create_command_cannot_make_a_tag_and_says_which_is_latest() -> None:
    latest = sync.Release(tag="v1.0.0", title="v1.0.0", notes="n", latest=True, prerelease=False)
    older = sync.Release(tag="v0.9.0", title="v0.9.0", notes="n", latest=False, prerelease=True)

    command = sync.create_command(latest, "octo/longhaul")
    assert command[:4] == ["gh", "release", "create", "v1.0.0"]
    # Without --verify-tag, gh would make a new tag on the default branch for a missing name.
    assert "--verify-tag" in command
    assert command[command.index("--repo") + 1] == "octo/longhaul"
    assert command[command.index("--title") + 1] == "v1.0.0"
    assert command[command.index("--notes-file") + 1] == "-"
    assert "--latest=true" in command and "--prerelease" not in command
    assert {"--latest=false", "--prerelease"} <= set(sync.create_command(older, "octo/longhaul"))


# ---------- the script against a real git repo and a fake gh ----------
FAKE_GH = """\
#!/bin/sh
# Answers the two calls the script makes. GH_STATE holds the tags that have a release.
case "$1" in
  api) cat "$GH_STATE" ;;
  release)
    printf '%s\\n' "$3" >> "$GH_STATE"
    printf 'ARGS %s\\n' "$*" >> "$GH_LOG"
    cat >> "$GH_LOG"
    printf '\\nEND\\n' >> "$GH_LOG"
    ;;
  *) exit 9 ;;
esac
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=git_env(repo))


def git_env(home: Path) -> dict[str, str]:
    # A home of its own, so a signing or hook setting of the machine does not reach the tags.
    return {
        **os.environ,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    git(repo, "tag", "-a", "v0.1.0", "-m", "v0.1.0: core")
    git(repo, "commit", "-q", "--allow-empty", "-m", "second")
    git(repo, "tag", "-a", "v0.10.0", "-m", "v0.10.0: later", "-m", "Body line one.\nLine two.")
    git(repo, "tag", "-a", "v0.9.0", "-m", "v0.9.0: middle")
    git(repo, "tag", "nightly")
    return repo


def run_script(repo: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    (tmp_path / "state").touch()
    env = {
        **git_env(repo),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_STATE": str(tmp_path / "state"),
        "GH_LOG": str(tmp_path / "gh.log"),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", "octo/longhaul", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def created(tmp_path: Path) -> list[tuple[str, str]]:
    log = tmp_path / "gh.log"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    return re.findall(r"^ARGS ([^\n]*)\n(.*?)\nEND\n", text, re.S | re.M)


def test_the_script_creates_each_missing_release_once(repo: Path, tmp_path: Path) -> None:
    first = run_script(repo, tmp_path)

    assert first.returncode == 0, first.stderr
    calls = created(tmp_path)
    assert [args.split()[2] for args, _ in calls] == ["v0.1.0", "v0.9.0", "v0.10.0"]
    assert [notes for _, notes in calls] == [
        "v0.1.0: core",
        "v0.9.0: middle",
        "v0.10.0: later\n\nBody line one.\nLine two.",
    ]
    assert "--latest=true" in calls[2][0]
    assert all("--latest=false" in args for args, _ in calls[:2])

    second = run_script(repo, tmp_path)

    assert second.returncode == 0, second.stderr
    assert len(created(tmp_path)) == 3
    assert second.stdout.strip() == "every version tag has a release"


def test_a_dry_run_creates_nothing(repo: Path, tmp_path: Path) -> None:
    result = run_script(repo, tmp_path, "--dry-run")

    assert result.returncode == 0, result.stderr
    assert created(tmp_path) == []
    assert "would create v0.10.0 (latest)" in result.stdout


# ---------- the workflow ----------
def workflow() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def triggers(data: dict[str, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key `on` as the boolean True.
    found = data.get("on", data.get(True))
    assert isinstance(found, dict)
    return found


def test_the_workflow_runs_on_a_version_tag_on_main_and_by_hand_only() -> None:
    on = triggers(workflow())

    assert set(on) == {"push", "workflow_dispatch"}
    assert on["push"]["branches"] == ["main"]
    assert on["push"]["tags"] == ["v*.*.*"]


def test_only_the_release_job_writes_and_only_to_contents() -> None:
    data = workflow()

    assert data["permissions"] == {"contents": "read"}
    assert {name: job["permissions"] for name, job in data["jobs"].items()} == {
        "releases": {"contents": "write"}
    }
    # One run at a time, so two runs never both create the same release. None is cut off.
    assert data["concurrency"] == {"group": "release", "cancel-in-progress": False}


def test_the_job_reads_every_annotated_tag_then_runs_the_script_with_the_run_token() -> None:
    steps = workflow()["jobs"]["releases"]["steps"]
    checkout = next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))
    runs = [" ".join(str(s.get("run", "")).split()) for s in steps]
    fetch = next(i for i, run in enumerate(runs) if run.startswith("git fetch --force --tags"))
    script = next(i for i, run in enumerate(runs) if "scripts/sync_releases.py" in run)

    # All the history and every tag, and no token left behind in the git config.
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    assert fetch < script
    assert runs[script].startswith("python3 scripts/sync_releases.py")
    assert "--dry-run" not in runs[script]
    assert steps[script]["env"] == {"GH_TOKEN": "${{ github.token }}"}
