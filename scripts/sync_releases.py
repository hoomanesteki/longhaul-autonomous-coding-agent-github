"""Make sure every version tag has a GitHub Release.

.github/workflows/release.yml runs this on each push of a version tag, on each push to main and
by hand. For each tag `vX.Y.Z` (or `vX.Y.Z-pre`) that has no release, it creates one titled
with the tag, with the tag's annotated message as the notes. The highest plain version is
marked as the latest release, and every other new release is marked as not the latest, so a
tag for an older line never takes that place. A tag that already has a release is left alone,
so a second run changes nothing.

The release workflow runs it with the runner's python3, so it uses only the standard library.
It reads the tags with git and talks to GitHub with the gh CLI, which reads GH_TOKEN.

    python3 scripts/sync_releases.py --repo OWNER/REPO [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass

# A NUL before each field: a ref name and a tag message cannot hold one, so the split is exact.
TAG_FORMAT = "%00%(refname:strip=2)%00%(contents:subject)%00%(contents:body)"
_VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


@dataclass(frozen=True)
class Tag:
    name: str
    message: str


@dataclass(frozen=True)
class Release:
    tag: str
    title: str
    notes: str
    latest: bool
    prerelease: bool


def version_key(name: str) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]] | None:
    """A sort key for a version tag, or None when the name is not one.

    A pre-release sorts below the release of the same number, as in semantic versioning.
    """
    found = _VERSION.fullmatch(name)
    if not found:
        return None
    major, minor, patch, pre = found.groups()
    if pre is None:
        return int(major), int(minor), int(patch), 1, ()
    parts = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split("."))
    return int(major), int(minor), int(patch), 0, parts


def parse_tags(output: str) -> list[Tag]:
    """The tags in the output of `git for-each-ref --format=TAG_FORMAT refs/tags`."""
    fields = output.split("\0")[1:]
    if len(fields) % 3:
        raise ValueError("git for-each-ref output has a broken record")
    tags = []
    for i in range(0, len(fields), 3):
        name, subject, body = fields[i : i + 3]
        message = "\n\n".join(part for part in (subject.strip(), body.strip()) if part)
        tags.append(Tag(name=name, message=message))
    return tags


def plan_releases(tags: Iterable[Tag], existing: Iterable[str]) -> list[Release]:
    """The releases to create, oldest version first, so the newest is also created last."""
    tags, have = list(tags), set(existing)
    versions = {tag.name: key for tag in tags if (key := version_key(tag.name)) is not None}
    plain = [name for name, key in versions.items() if key[3] == 1]
    highest = max(plain, key=lambda name: versions[name]) if plain else None
    missing = sorted(
        (tag for tag in tags if tag.name in versions and tag.name not in have),
        key=lambda tag: versions[tag.name],
    )
    return [
        Release(
            tag=tag.name,
            title=tag.name,
            notes=tag.message or tag.name,
            latest=tag.name == highest,
            prerelease=versions[tag.name][3] == 0,
        )
        for tag in missing
    ]


def create_command(release: Release, repo: str) -> list[str]:
    # --verify-tag stops gh from making a new tag when the name is not on GitHub.
    command = ["gh", "release", "create", release.tag, "--repo", repo, "--verify-tag"]
    command += ["--title", release.title, "--notes-file", "-"]
    command.append(f"--latest={'true' if release.latest else 'false'}")
    if release.prerelease:
        command.append("--prerelease")
    return command


def _run(command: list[str], stdin: str | None = None) -> str:
    result = subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"{' '.join(command[:3])} failed: {result.stderr.strip()}")
    return result.stdout


def read_tags() -> list[Tag]:
    return parse_tags(_run(["git", "for-each-ref", f"--format={TAG_FORMAT}", "refs/tags"]))


def read_release_tags(repo: str) -> list[str]:
    path = f"repos/{repo}/releases?per_page=100"
    output = _run(["gh", "api", "--paginate", path, "--jq", ".[].tag_name"])
    return output.split()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when GITHUB_REPOSITORY is not set")

    plan = plan_releases(read_tags(), read_release_tags(args.repo))
    if not plan:
        print("every version tag has a release")
        return 0
    for release in plan:
        flags = " (latest)" if release.latest else ""
        flags += " (pre-release)" if release.prerelease else ""
        print(f"{'would create' if args.dry_run else 'creating'} {release.tag}{flags}")
        if not args.dry_run:
            _run(create_command(release, args.repo), stdin=release.notes)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
