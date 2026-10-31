"""Runtime guard for invariant I2: no activity input or output serializes above 256 KB.

Temporal keeps every activity input and output in the workflow history. Large payloads slow
every replay and eventually hit the server's limits (ADR-0005). Large data goes to the blob
store and only a BlobRef crosses the boundary. This guard turns a mistake into an immediate
error in the activity that made it, instead of a failure inside Temporal later.

The size is measured with pydantic's JSON encoder, which writes non-ASCII text as UTF-8, so the
count is in bytes, not characters. Another encoder can differ by a small factor for text full
of non-ASCII characters. The limit is far below the server's, so that cannot matter.
"""

from __future__ import annotations

from typing import Any

from pydantic_core import to_json

DEFAULT_LIMIT = 256_000


class PayloadTooLargeError(ValueError):
    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"payload is {size} bytes of JSON, over the limit of {limit} bytes; "
            "store it in the blob store and pass a BlobRef instead"
        )
        self.size = size
        self.limit = limit

    def __reduce__(self) -> tuple[Any, ...]:
        # Exception pickling replays only self.args, which would drop size and limit.
        return (type(self), (self.size, self.limit))


def json_size(obj: object) -> int:
    """Bytes of JSON that obj serializes to: a pydantic model or any plain JSON value."""
    return len(to_json(obj))


def assert_small(obj: object, limit: int = DEFAULT_LIMIT) -> None:
    """Raise PayloadTooLargeError if obj serializes to more than limit bytes of JSON."""
    size = json_size(obj)
    if size > limit:
        raise PayloadTooLargeError(size, limit)
