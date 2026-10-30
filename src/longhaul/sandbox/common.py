"""Rules the Docker sandbox and the fake sandbox share.

Both classes build on these, so a unit test against the fake says the same
thing as the integration test against Docker.
"""

from __future__ import annotations

import posixpath

WORKDIR = "/workspace"
# The places a command can write to. read_file and write_file stay inside them.
# Docker's archive API only sees WORKDIR, a volume: it cannot reach the tmpfs
# mounts, so the Docker sandbox refuses those two even though they are listed.
WRITABLE_MOUNTS = (WORKDIR, "/tmp", "/home/agent")  # noqa: S108


class PathEscapeError(PermissionError, ValueError):
    """A path outside the places a sandbox lets callers touch.

    It is a PermissionError because that is what the Docker sandbox raised and a
    ValueError because that is what the fake raised, so callers of either keep working.
    """


def resolve_path(path: str) -> str:
    joined = path if path.startswith("/") else posixpath.join(WORKDIR, path)
    return posixpath.normpath(joined)


def mount_for(path: str) -> str:
    """The mount that holds `path`. Raises PathEscapeError for anything else."""
    for mount in WRITABLE_MOUNTS:
        if path == mount or path.startswith(mount + "/"):
            return mount
    raise PathEscapeError(f"path is not on a writable mount: {path}")
