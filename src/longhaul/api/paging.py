"""Cursors for lists, and pages of lines for long text.

A cursor is a few values packed into one URL-safe string. The console treats it as opaque and
sends back the last one it got. The values are the sort key of the last row of a page, so the
next page starts after it with a comparison on that key (keyset pagination). An offset would
skip or repeat rows when a task writes new ones between two requests, and a running task does.

A page of text is a range of lines of a tool output, counted the way `read_output` counts them
(context.truncate.split_lines): only a newline ends a line. The text is cut by line count and
by size, so that one request cannot return a build log of 50 MB.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass

from longhaul.context.truncate import split_lines

__all__ = [
    "MAX_CHARS",
    "MAX_LINES",
    "BeyondEndError",
    "CursorError",
    "RangeError",
    "TextPage",
    "decode_cursor",
    "encode_cursor",
    "slice_lines",
]

# The most lines and characters one page of a tool output holds. The console asks for the next
# page with `start` set to `next_start`.
MAX_LINES = 2_000
MAX_CHARS = 200_000


class CursorError(ValueError):
    """The cursor is not one this API made."""


class RangeError(ValueError):
    """The range of lines asked for cannot be read."""


class BeyondEndError(RangeError):
    """The range starts after the last line of the text."""


def encode_cursor(parts: Sequence[str | int]) -> str:
    raw = json.dumps(list(parts), separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, size: int) -> list[str | int]:
    """The `size` values a cursor was made from. Raises CursorError for anything else."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        parts = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error):
        raise CursorError("the cursor is not valid") from None
    if (
        not isinstance(parts, list)
        or len(parts) != size
        or not all(isinstance(part, str | int) and not isinstance(part, bool) for part in parts)
    ):
        raise CursorError("the cursor is not valid")
    return parts


@dataclass(frozen=True)
class TextPage:
    """Lines `start_line` to `end_line` of a text of `total_lines`.

    `next_start` is the line to ask for next when the page ended before the range did, and
    None when nothing was left out.
    """

    text: str
    start_line: int
    end_line: int
    total_lines: int
    next_start: int | None


def slice_lines(text: str, start: int, end: int | None) -> TextPage:
    """The lines from `start` to `end` (both 1-based and inclusive; `end` None means to the end).

    A range that asks for more than MAX_LINES lines or MAX_CHARS characters gets the first part,
    cut on a line, and `next_start` says where to go on. A single line longer than MAX_CHARS is
    cut inside the line, since no cut between lines would fit it.
    """
    if end is not None and end < start:
        raise RangeError(f"end {end} is before start {start}")
    lines = split_lines(text)
    total = len(lines)
    if total == 0:
        if start > 1:
            raise BeyondEndError(f"start {start} is beyond the end of the output (it is empty)")
        return TextPage("", 1, 0, 0, None)
    if start > total:
        raise BeyondEndError(f"start {start} is beyond the end of the output ({total} lines)")
    last = total if end is None else min(end, total)
    chosen = lines[start - 1 : min(last, start + MAX_LINES - 1)]
    kept: list[str] = []
    size = 0
    for line in chosen:
        # The newline between two lines counts as well.
        if kept and size + len(line) + 1 > MAX_CHARS:
            break
        kept.append(line)
        size += len(line) + 1
    page = "\n".join(kept)
    if len(page) > MAX_CHARS:  # one line that is longer than a page
        page = page[:MAX_CHARS]
    reached = start + len(kept) - 1
    return TextPage(page, start, reached, total, reached + 1 if reached < last else None)
