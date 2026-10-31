"""Turn a fixture folder into a git repo with one commit and a `base` tag.

Fixtures stay plain folders in this repo. Tests and the CLI copy one into a
fresh directory and commit it there.

The result is the same on every machine: the commit id depends on the files and
nothing else. Later stories hand the repo to a fake GitHub and record model
requests that mention the base commit, so a build must not vary with the clock,
the host's default branch name, or the user's git settings.
"""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "repos"

# The reference fix must never ship inside the repo the agent works in. .DS_Store is
# Finder debris that would change the commit id on one machine only.
_IGNORE = shutil.ignore_patterns(
    "REFERENCE.patch", "__pycache__", ".pytest_cache", "*.egg-info", ".DS_Store"
)
# TaskSpec.base_branch defaults to "main", and a bare `git init` follows the host's setting.
_BRANCH = "main"
_DATE = "2000-01-01T00:00:00+00:00"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "longhaul-fixture",
    "GIT_AUTHOR_EMAIL": "fixture@localhost",
    "GIT_AUTHOR_DATE": _DATE,
    "GIT_COMMITTER_NAME": "longhaul-fixture",
    "GIT_COMMITTER_EMAIL": "fixture@localhost",
    "GIT_COMMITTER_DATE": _DATE,
}
# Settings that would otherwise come from the user's config: signing, hooks, line endings.
# The last two name files git reads from ~/.config/git even when no gitconfig is read, so
# GIT_CONFIG_GLOBAL does not cover them. Global ignore rules would drop files from the
# commit, and global attributes could rewrite line endings in it.
_CONFIG = {
    "core.hooksPath": os.devnull,
    "commit.gpgsign": "false",
    "tag.gpgsign": "false",
    "core.autocrlf": "false",
    "core.excludesFile": os.devnull,
    "core.attributesFile": os.devnull,
}
_SETTINGS = [arg for key, value in _CONFIG.items() for arg in ("-c", f"{key}={value}")]


def isolated_git_env() -> dict[str, str]:
    """Environment for git calls: no user or system config, nothing from a running hook.

    A git hook exports GIT_DIR and friends. Left in place they would point these calls
    at the hook's repository instead of the new one.
    """
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {
        **inherited,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        **_IDENTITY,
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *_SETTINGS, *args],
        check=True,
        capture_output=True,
        text=True,
        env=isolated_git_env(),
    )


def make_fixture_git(src_dir: Path, dest_dir: Path) -> Path:
    """Copy src_dir to dest_dir, commit it as "initial" and tag the commit "base"."""
    if dest_dir.exists():
        raise FileExistsError(dest_dir)
    shutil.copytree(src_dir, dest_dir, ignore=_IGNORE)
    # An empty template keeps hooks and other files from the host's template directory out.
    _git(dest_dir, "init", "-q", "-b", _BRANCH, "--template=")
    _git(dest_dir, "add", "-A")
    _git(dest_dir, "commit", "-q", "--no-verify", "-m", "initial")
    _git(dest_dir, "tag", "base")
    return dest_dir


def main(argv: list[str] | None = None) -> int:
    available = sorted(p.name for p in FIXTURES_ROOT.iterdir() if (p / "pyproject.toml").is_file())
    parser = argparse.ArgumentParser(description="Create a git repo from a fixture.")
    parser.add_argument("name", choices=available, help="fixture name")
    parser.add_argument("dest", type=Path, help="directory to create (must not exist)")
    args = parser.parse_args(argv)
    repo = make_fixture_git(FIXTURES_ROOT / args.name, args.dest)
    print(repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
