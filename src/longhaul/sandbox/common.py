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


# Each stream keeps at most this much (the 1 MB cap from LH-107): the first half
# and the last half. Tools that trim further (LH-109 keeps 8,000 plus 12,000
# characters) need the real tail, so the middle goes, not the end.
OUTPUT_CAP = 1_000_000
OUTPUT_HEAD = OUTPUT_CAP // 2
OUTPUT_TAIL = OUTPUT_CAP - OUTPUT_HEAD


def check_timeout(timeout_s: float) -> None:
    """GNU timeout reads 0 as "no limit", which would switch the F7 guarantee off."""
    if timeout_s <= 0:
        raise ValueError(f"timeout_s must be positive, got {timeout_s}")


class BoundedOutput:
    """Collects a stream but keeps only its first and last bytes.

    Memory stays at roughly the cap however much the command prints, and the
    stream is still read to the end so the command never blocks on a full pipe.
    """

    def __init__(self, head: int = OUTPUT_HEAD, tail: int = OUTPUT_TAIL) -> None:
        self._head_limit = head
        self._tail_limit = tail
        self._head = bytearray()
        self._tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self._head_limit - len(self._head)
        if room > 0:
            self._head += chunk[:room]
            chunk = chunk[room:]
        if chunk and self._tail_limit:
            self._tail += chunk
            # Trimmed in batches so a long stream costs linear time.
            if len(self._tail) > 2 * self._tail_limit:
                del self._tail[: -self._tail_limit]

    def text(self) -> str:
        tail = bytes(self._tail[-self._tail_limit :]) if self._tail_limit else b""
        omitted = self.total - len(self._head) - len(tail)
        head = bytes(self._head)
        if omitted <= 0:
            return (head + tail).decode("utf-8", errors="replace")
        marker = f"\n[... {omitted} bytes omitted ...]\n"
        return (
            head.decode("utf-8", errors="replace") + marker + tail.decode("utf-8", errors="replace")
        )
