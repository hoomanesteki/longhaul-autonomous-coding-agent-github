"""Path safety for file tools. Every path must stay inside /workspace."""

from __future__ import annotations

import posixpath
import shlex

from longhaul.contracts import Sandbox

WORKDIR = "/workspace"


class PathError(Exception):
    """A path the tools refuse to touch."""


def normalize(raw: str) -> str:
    """Return a workspace-relative path, or raise PathError on an escape."""
    path = raw.strip() or "."
    if path.startswith("/"):
        if path != WORKDIR and not path.startswith(WORKDIR + "/"):
            raise PathError(f"path is outside {WORKDIR}: {raw}")
        path = path[len(WORKDIR) :].lstrip("/") or "."
    norm = posixpath.normpath(path)
    if norm == ".." or norm.startswith("../"):
        raise PathError(f"path is outside {WORKDIR}: {raw}")
    return norm


def check_resolved(resolved: str, root: str) -> None:
    """Raise PathError unless `resolved` is `root` or below it."""
    if resolved != root and not resolved.startswith(root.rstrip("/") + "/"):
        raise PathError(f"path resolves outside {WORKDIR}: {resolved}")


_RESOLVE = (
    "python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]));"
    'print(os.path.realpath("."));print(int(os.path.exists(sys.argv[1])))\' '
)


async def safe_path(sandbox: Sandbox, raw: str, *, must_exist: bool = False) -> str:
    """Normalize `raw`, follow symlinks inside the sandbox and return the relative path.

    The sandbox working directory is the workspace root, so relative paths
    work on both the Docker and the fake sandbox.
    """
    rel = normalize(raw)
    res = await sandbox.exec(_RESOLVE + shlex.quote(rel), timeout_s=15)
    parts = res.stdout.split("\n")
    if res.exit_code != 0 or len(parts) < 3:
        raise PathError(f"could not resolve path: {raw}")
    resolved, root, exists = parts[0], parts[1], parts[2]
    check_resolved(resolved, root)
    if must_exist and exists != "1":
        raise FileNotFoundError(f"file not found: {rel}")
    return rel
