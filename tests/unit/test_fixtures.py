import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "repos"

_spec = importlib.util.spec_from_file_location(
    "make_fixture_git", ROOT / "scripts" / "make_fixture_git.py"
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
make_fixture_git = _module.make_fixture_git

FIXTURE_NAMES = ["calc", "textkit", "inventory"]

# The failing tests each fixture must have at base, and that ISSUE.md names.
EXPECTED_FAILURES = {
    "calc": {"tests/test_calc.py::test_add_positive_numbers"},
    "textkit": {"tests/test_slug.py::test_slugify_accented_latin"},
    "inventory": {"tests/test_store.py::test_remove_more_than_stock_is_rejected"},
}


def _run_pytest(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rf", "--tb=no", "-p", "no:cacheprovider"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _failed_ids(output: str) -> set[str]:
    return set(re.findall(r"^FAILED (\S+)", output, flags=re.MULTILINE))


def _git_env() -> dict[str, str]:
    """Git as these tests read it: no user or system config, nothing inherited from a hook."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    return env


def _git_out(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    return result.stdout.strip()


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_fails_at_base(name: str, tmp_path: Path) -> None:
    repo = make_fixture_git(FIXTURES / name, tmp_path / name)
    result = _run_pytest(repo)
    assert result.returncode == 1, result.stdout + result.stderr
    assert _failed_ids(result.stdout) == EXPECTED_FAILURES[name]
    issue = (FIXTURES / name / "ISSUE.md").read_text(encoding="utf-8")
    for test_id in EXPECTED_FAILURES[name]:
        assert test_id.split("::")[1] in issue


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_reference_patch_passes(name: str, tmp_path: Path) -> None:
    repo = make_fixture_git(FIXTURES / name, tmp_path / name)
    subprocess.run(
        ["git", "-C", str(repo), "apply", str(FIXTURES / name / "REFERENCE.patch")],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(),
    )
    result = _run_pytest(repo)
    assert result.returncode == 0, result.stdout


def test_make_fixture_git_tags_base(tmp_path: Path) -> None:
    repo = make_fixture_git(FIXTURES / "calc", tmp_path / "calc")
    assert _git_out(repo, "tag", "--list", "base") == "base"
    assert _git_out(repo, "log", "-1", "--format=%an <%ae>|%s") == (
        "longhaul-fixture <fixture@localhost>|initial"
    )
    assert not (repo / "REFERENCE.patch").exists()


# --- the same fixture builds the same repo on every machine --------------------------------

# A machine whose own git settings would break a helper that reads them.
HOSTILE_CONFIG = """\
[init]
defaultBranch = master
[commit]
gpgSign = true
[tag]
gpgSign = true
[gpg]
program = /nonexistent/gpg
[core]
hooksPath = {hooks}
autocrlf = true
[user]
name = Somebody Else
email = else@example.com
"""


def _host_git_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Make git read `body` as the user's and the system's config."""
    config = tmp_path / "host.gitconfig"
    config.write_text(body, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))


def _failing_hooks(directory: Path) -> Path:
    directory.mkdir()
    for name in ("pre-commit", "commit-msg", "post-commit"):
        hook = directory / name
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
    return directory


def test_two_builds_give_the_same_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A caller can have commit dates in its environment. They must not reach the fixture.
    for stamp in ("2011-11-11T11:11:11+00:00", "2022-02-02T02:02:02+00:00"):
        monkeypatch.setenv("GIT_AUTHOR_DATE", stamp)
        monkeypatch.setenv("GIT_COMMITTER_DATE", stamp)
        make_fixture_git(FIXTURES / "calc", tmp_path / stamp[:4])
    first, second = tmp_path / "2011", tmp_path / "2022"
    assert _git_out(first, "rev-parse", "HEAD") == _git_out(second, "rev-parse", "HEAD")
    assert _git_out(first, "rev-parse", "base") == _git_out(first, "rev-parse", "HEAD")
    for fmt in ("%aI", "%cI"):
        assert _git_out(first, "log", "-1", f"--format={fmt}") == _git_out(
            second, "log", "-1", f"--format={fmt}"
        )


def test_branch_is_main_whatever_the_host_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_git_config(tmp_path, monkeypatch, "[init]\ndefaultBranch = master\n")
    repo = make_fixture_git(FIXTURES / "calc", tmp_path / "calc")
    assert _git_out(repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_build_ignores_the_users_git_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clean = make_fixture_git(FIXTURES / "calc", tmp_path / "clean")
    hooks = _failing_hooks(tmp_path / "hooks")
    _host_git_config(tmp_path, monkeypatch, HOSTILE_CONFIG.format(hooks=hooks))
    hostile = make_fixture_git(FIXTURES / "calc", tmp_path / "hostile")
    assert _git_out(hostile, "rev-parse", "base") == _git_out(clean, "rev-parse", "base")
    assert _git_out(hostile, "log", "-1", "--format=%an <%ae>") == (
        "longhaul-fixture <fixture@localhost>"
    )


def test_build_ignores_the_users_ignore_and_attributes_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git reads these two files from the user's config directory even when it reads no
    # gitconfig. An ignore rule drops files from the commit, and `text=auto` rewrites CRLF.
    source = tmp_path / "source"
    source.mkdir()
    (source / "NOTES.md").write_text("notes\n", encoding="utf-8")
    (source / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    clean = make_fixture_git(source, tmp_path / "clean")

    config_dir = tmp_path / "home" / ".config" / "git"
    config_dir.mkdir(parents=True)
    (config_dir / "ignore").write_text("*.md\n", encoding="utf-8")
    (config_dir / "attributes").write_text("* text=auto\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home" / ".config"))
    hostile = make_fixture_git(source, tmp_path / "hostile")

    assert _git_out(hostile, "ls-files") == _git_out(clean, "ls-files")
    assert _git_out(hostile, "rev-parse", "base:crlf.txt") == _git_out(
        clean, "rev-parse", "base:crlf.txt"
    )
    assert _git_out(hostile, "rev-parse", "base") == _git_out(clean, "rev-parse", "base")


def test_build_ignores_git_variables_set_by_a_running_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `make check` can run from a git hook, which exports these for its own repository.
    elsewhere = tmp_path / "elsewhere.git"
    monkeypatch.setenv("GIT_DIR", str(elsewhere))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "elsewhere-tree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "elsewhere-index"))
    repo = make_fixture_git(FIXTURES / "calc", tmp_path / "calc")
    assert (repo / ".git").is_dir()
    assert not elsewhere.exists()
    assert _git_out(repo, "tag", "--list", "base") == "base"


def test_finder_debris_is_not_committed(tmp_path: Path) -> None:
    # A .DS_Store in the source folder would change the commit id on that one machine.
    source = tmp_path / "calc-src"
    shutil.copytree(FIXTURES / "calc", source)
    (source / ".DS_Store").write_bytes(b"\x00")
    repo = make_fixture_git(source, tmp_path / "calc")
    assert ".DS_Store" not in _git_out(repo, "ls-files").splitlines()
