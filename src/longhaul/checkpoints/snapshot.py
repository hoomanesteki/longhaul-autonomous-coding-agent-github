"""A plain folder as a repo with one commit, the same on every machine.

`GitCheckpointStore.init_task` clones a repo at a commit that it is given. A `local:` repo is a
folder, not a repo, so prepare_workspace commits it first, here. The commit id depends on the
files and on nothing else, the way scripts/make_fixture_git.py builds the fixtures: a fixed
author, committer, date and message, no user or system git config, an empty template and no line
ending conversion. A retried prepare_workspace therefore gets the base it had before, and a
person can rebuild the sha by hand to check a log line.

Which files go in is not decided here. agent/workspace.py picks them (the built-in patterns and
.longhaulignore), so the sandbox of a local run and the repo of a durable run hold the same
tree. They are added with --force. A restore deletes whatever the commit lacks, so a file that
the copy rules kept has to be in it, also when the folder's own .gitignore names it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from longhaul.agent.workspace import SourceFile, WorkspaceError

BRANCH = "main"  # TaskSpec.base_branch defaults to it
MESSAGE = "base snapshot"
GIT_TIMEOUT_S = 120

_DATE = "2000-01-01T00:00:00+00:00"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "longhaul-fixture",
    "GIT_AUTHOR_EMAIL": "fixture@localhost",
    "GIT_AUTHOR_DATE": _DATE,
    "GIT_COMMITTER_NAME": "longhaul-fixture",
    "GIT_COMMITTER_EMAIL": "fixture@localhost",
    "GIT_COMMITTER_DATE": _DATE,
}
# Settings that would otherwise come from the user's config: signing, hooks, line endings and
# the global ignore and attributes files, which would drop files from the commit or rewrite them.
_CONFIG = {
    "core.hooksPath": os.devnull,
    "commit.gpgsign": "false",
    "tag.gpgsign": "false",
    "core.autocrlf": "false",
    "core.excludesFile": os.devnull,
    "core.attributesFile": os.devnull,
}
_SETTINGS = [arg for key, value in _CONFIG.items() for arg in ("-c", f"{key}={value}")]


def _environment() -> dict[str, str]:
    """No user or system config, and nothing that a running git hook exports.

    A hook exports GIT_DIR and friends. Left in place they would point these calls at the repo
    that runs the hook, and not at the new one.
    """
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {
        **inherited,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        **_IDENTITY,
    }


async def _git(repo: Path, *args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo),
        *_SETTINGS,
        *args,
        env=_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT_S)
    except BaseException:  # a timeout, or the activity was cancelled: git must not keep running
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        detail = err.decode(errors="replace").strip()[-800:]
        raise WorkspaceError(f"git {args[0]} failed while the base commit was made: {detail}")
    return out.decode(errors="replace").strip()


def _copy(source_dir: Path, files: Sequence[SourceFile], dest: Path) -> None:
    for item in files:
        target = dest / item.path
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source_dir / item.path, target)
        except OSError as exc:
            raise WorkspaceError(f"cannot read {item.path}: {exc.strerror or exc}") from exc
        # Git records only the execute bit, and the copy takes its other bits from the umask of
        # this process. Setting both here keeps the commit the same on every machine.
        target.chmod(0o755 if item.executable else 0o644)


async def snapshot_folder(source_dir: Path, files: Sequence[SourceFile], dest: Path) -> str:
    """Copy `files` of `source_dir` to `dest`, commit them there and return the commit's sha.

    `dest` must not exist. A failure removes what was made, so no half-built repo is left.
    """
    if dest.exists():
        raise FileExistsError(dest)
    try:
        await asyncio.to_thread(_copy, source_dir, files, dest)
        # An empty template keeps hooks and other files from the host's template folder out.
        await _git(dest, "init", "-q", "-b", BRANCH, "--template=")
        await _git(dest, "add", "-A", "--force")
        await _git(dest, "commit", "-q", "--no-verify", "-m", MESSAGE)
        return await _git(dest, "rev-parse", "HEAD")
    except BaseException:
        await asyncio.to_thread(shutil.rmtree, dest, True)
        raise
