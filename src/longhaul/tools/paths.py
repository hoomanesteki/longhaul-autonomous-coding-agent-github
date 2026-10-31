"""Path safety for file tools. Every path must stay inside /workspace.

A path is resolved once, inside the sandbox, with symlinks followed. The result
holds the real path below the workspace root. That is the only form a tool may
pass on, to the sandbox's file calls and to commands. Two reasons:

- Docker's archive calls do not follow a link in the last part of a path, and a
  write through a linked directory replaces the link with a real directory.
- The check and the use then look at the same path, and a leading dash can only
  come from a real file name, never from a link or from the model's spelling.
"""

from __future__ import annotations

import json
import posixpath
import shlex
from dataclasses import dataclass
from typing import Literal

from longhaul.contracts import Sandbox
from longhaul.tools import ToolError, to_utf8

WORKDIR = "/workspace"
# What a Linux filesystem accepts. The sandbox raises an OSError for more on the fake and
# an APIError on Docker, and the registry lets both through as faults of the sandbox.
MAX_NAME_BYTES = 255
MAX_PATH_BYTES = 4096

Kind = Literal["file", "dir", "other", "missing"]
_KINDS = ("file", "dir", "other", "missing")


class PathError(ToolError):
    """A path the tools refuse to touch."""


@dataclass(frozen=True)
class Resolved:
    """A path after the sandbox followed its symlinks.

    `requested` is what the model asked for, normalized. Error messages quote it.
    `rel` is the real path below the workspace root, "." for the root itself.
    """

    requested: str
    rel: str
    kind: Kind

    def require_exists(self) -> None:
        if self.kind == "missing":
            raise FileNotFoundError(f"path not found: {self.requested}")

    def require_dir(self) -> None:
        self.require_exists()
        if self.kind == "file":
            raise PathError(f"{self.requested} is a file; use read_file to read it")
        if self.kind != "dir":
            raise PathError(f"{self.requested} is not a directory")

    def require_file(self) -> None:
        """For tools that read a file that must be there."""
        if self.kind == "missing":
            raise FileNotFoundError(f"file not found: {self.requested}")
        if self.kind == "dir":
            raise PathError(f"{self.requested} is a directory; use list_dir to see what is in it")
        self.require_writable()

    def require_writable(self) -> None:
        """For tools that write a file that may or may not be there yet."""
        if self.kind == "dir":
            raise PathError(f"{self.requested} is a directory, not a file")
        if self.kind == "other":
            raise PathError(f"{self.requested} is not a regular file")


def normalize(raw: str) -> str:
    """Return a workspace-relative path, or raise PathError on an escape."""
    if "\0" in raw:
        raise PathError("path contains a NUL byte")
    to_utf8(raw, "the path", error=PathError)
    path = raw.strip() or "."
    if path.startswith("/"):
        if path != WORKDIR and not path.startswith(WORKDIR + "/"):
            raise PathError(f"path is outside {WORKDIR}: {raw}")
        path = path[len(WORKDIR) :].lstrip("/") or "."
    norm = posixpath.normpath(path)
    if norm == ".." or norm.startswith("../"):
        raise PathError(f"path is outside {WORKDIR}: {raw}")
    if len(norm.encode()) > MAX_PATH_BYTES:
        raise PathError(f"the path is longer than {MAX_PATH_BYTES} bytes")
    for name in norm.split("/"):
        if len(name.encode()) > MAX_NAME_BYTES:
            raise PathError(
                f"a name in the path is longer than {MAX_NAME_BYTES} bytes: {name[:40]}..."
            )
    return norm


def check_resolved(resolved: str, root: str) -> None:
    """Raise PathError unless `resolved` is `root` or below it."""
    if resolved != root and not resolved.startswith(root.rstrip("/") + "/"):
        raise PathError(f"path resolves outside {WORKDIR}: {resolved}")


# Runs in the sandbox. It prints the real path, the real workspace root and what
# the real path is, as JSON so that odd file names cannot break the parsing. The
# sandbox working directory is the workspace root on both the Docker and the fake
# sandbox, so a relative path needs no prefix. Double quotes only, so the whole
# script survives shlex.quote as one word.
_RESOLVE = (
    "import json,os,sys;p=os.path.realpath(sys.argv[1]);"
    'k="dir" if os.path.isdir(p) else "file" if os.path.isfile(p) '
    'else "other" if os.path.lexists(p) else "missing";'
    'print(json.dumps([p,os.path.realpath("."),k]))'
)


def _parse_resolution(stdout: str) -> tuple[str, str, Kind] | None:
    try:
        resolved, root, kind = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if isinstance(resolved, str) and isinstance(root, str) and kind in _KINDS:
        return resolved, root, kind
    return None


async def resolve(sandbox: Sandbox, raw: str) -> Resolved:
    """Normalize `raw`, follow symlinks inside the sandbox and refuse an escape."""
    requested = normalize(raw)
    cmd = f"python3 -c {shlex.quote(_RESOLVE)} {shlex.quote(requested)}"
    res = await sandbox.exec(cmd, timeout_s=15)
    parsed = _parse_resolution(res.stdout) if res.exit_code == 0 else None
    if parsed is None:
        raise PathError(f"could not resolve path: {raw}")
    resolved, root, kind = parsed
    check_resolved(resolved, root)
    return Resolved(requested, posixpath.relpath(resolved, root), kind)


async def safe_path(sandbox: Sandbox, raw: str, *, must_exist: bool = False) -> str:
    """The real path of `raw` below the workspace root, or PathError if it escapes."""
    found = await resolve(sandbox, raw)
    if must_exist:
        found.require_exists()
    return found.rel
