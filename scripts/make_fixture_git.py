"""Turn a fixture folder into a git repo with one commit and a `base` tag.

Fixtures stay plain folders in this repo. Tests and the CLI copy one into a
fresh directory and commit it there.
"""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

FIXTURES_ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "repos"

# The reference fix must never ship inside the repo the agent works in.
_IGNORE = shutil.ignore_patterns("REFERENCE.patch", "__pycache__", ".pytest_cache", "*.egg-info")
_IDENTITY = {
    "GIT_AUTHOR_NAME": "longhaul-fixture",
    "GIT_AUTHOR_EMAIL": "fixture@localhost",
    "GIT_COMMITTER_NAME": "longhaul-fixture",
    "GIT_COMMITTER_EMAIL": "fixture@localhost",
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_IDENTITY},
    )


def make_fixture_git(src_dir: Path, dest_dir: Path) -> Path:
    """Copy src_dir to dest_dir, commit it as "initial" and tag the commit "base"."""
    if dest_dir.exists():
        raise FileExistsError(dest_dir)
    shutil.copytree(src_dir, dest_dir, ignore=_IGNORE)
    _git(dest_dir, "init", "-q")
    _git(dest_dir, "add", "-A")
    _git(dest_dir, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "initial")
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
