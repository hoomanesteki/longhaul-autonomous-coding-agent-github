"""Git tools. Diffs are taken against the `base` tag the workspace gets when it is prepared.

Reading the diff never changes the repository: no staging, no commits, no
intent-to-add entries. New files are found with `git ls-files` and shown with
`git diff --no-index`, so the index stays exactly as the agent left it.
"""

from __future__ import annotations

import shlex
from typing import Any

from longhaul.contracts import ExecResult, Sandbox, ToolContext, ToolResult, ToolSpec
from longhaul.tools.exec import cap_text
from longhaul.tools.files import IGNORED_DIRS, fail, ok

# Spelled out so a branch or a file that is also called "base" cannot be picked up instead.
BASE_REF = "refs/tags/base"
NO_CHANGES = "no changes against base"
GIT_TIMEOUT_S = 60
MAX_NEW_FILES = 100

# Untracked paths that are never part of the agent's work: the venv, caches and
# build metadata. Workspace preparation should list them in .git/info/exclude too.
# They are also left out here so the diff stays clean until it does.
NOISE_GLOBS = (
    *(f"**/{name}/**" for name in (*IGNORED_DIRS, ".pytest_cache", ".mypy_cache", ".ruff_cache")),
    "**/*.egg-info/**",
    "**/*.pyc",
)


def tracked_command(staged: bool) -> str:
    flag = "--staged " if staged else ""
    return f"git diff --no-color {flag}{BASE_REF} --"


def untracked_command() -> str:
    """List untracked files that are not ignored, NUL separated, noise excluded at the source."""
    excludes = " ".join(shlex.quote(f":(exclude,glob){glob}") for glob in NOISE_GLOBS)
    return f"git ls-files --others --exclude-standard -z -- . {excludes}"


def new_file_command(paths: list[str]) -> str:
    quoted = " ".join(shlex.quote(path) for path in paths)
    # `git diff --no-index` exits 1 when the files differ, so end the loop with true.
    return f'for f in {quoted}; do git diff --no-index --no-color -- /dev/null "$f"; done; true'


def _why_failed(res: ExecResult) -> str:
    if res.timed_out:
        return f"timed out after {GIT_TIMEOUT_S} s"
    return res.stderr.strip() or f"exit code {res.exit_code}"


class GitDiff:
    spec = ToolSpec(
        name="git_diff",
        description=(
            "Show your changes so far as a git diff against the starting point. "
            "Use it to review your work before you finish."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "staged": {
                    "type": "boolean",
                    "default": False,
                    "description": "Only show what is already staged with git add.",
                },
            },
            "additionalProperties": False,
        },
    )
    mutates = False

    def __init__(self, max_new_files: int = MAX_NEW_FILES) -> None:
        self.max_new_files = max_new_files

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        staged = bool(args.get("staged", False))
        tracked = await sandbox.exec(tracked_command(staged), timeout_s=GIT_TIMEOUT_S)
        if tracked.exit_code != 0 or tracked.timed_out:
            return fail(f"git diff failed: {_why_failed(tracked)}")
        text = tracked.stdout
        # The index holds no untracked files, so the staged view has nothing to add.
        if not staged:
            text += await self._new_files(sandbox)
        text = text.rstrip("\n")
        return ok(cap_text(text) if text else NO_CHANGES)

    async def _new_files(self, sandbox: Sandbox) -> str:
        listing = await sandbox.exec(untracked_command(), timeout_s=GIT_TIMEOUT_S)
        if listing.exit_code != 0 or listing.timed_out:
            return f"\n(could not list new files: {_why_failed(listing)})\n"
        paths = [p for p in listing.stdout.split("\0") if p]
        if not paths:
            return ""
        shown = paths[: self.max_new_files]
        diffs = await sandbox.exec(new_file_command(shown), timeout_s=GIT_TIMEOUT_S)
        text = diffs.stdout
        if len(paths) > len(shown):
            text += f"\n... {len(paths) - len(shown)} more untracked files not shown\n"
        return text
