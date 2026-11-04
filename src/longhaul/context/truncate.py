"""Shaping a long tool output into a head, a tail and a handle (LH-402).

One noisy test run can print ten thousand lines. Put in the transcript whole, it would fill the
window and be paid for again on every later turn. So the model reads the first 60 lines, the
last 60 lines and a marker that names a handle, and pages through the rest with read_output
(tools/output.py) only if it needs to. The head holds the command and the first failure, the
tail holds the summary line that test runners and compilers print last, and the middle is
usually repetition.

The cap is `context.tool_output_max_tokens` and the count is estimate_tokens, the same rule the
rest of the context code uses. Output at or under the cap is returned as it is. Output over it
becomes at most `max_tokens` by that estimate, marker included, because a result that is meant
to save the window must not be able to fill it. In the usual case that takes no more than the
cut of lines. Some outputs have lines so long that 120 of them are still over the cap, such as a
minified file or a log of one-line JSON. Then the head and the tail are cut by characters, half
of the room each. The marker still says how many lines and bytes the whole output has, and the
handle still gives all of it.

Lines are counted the way read_output pages them (split_lines): only "\\n" ends a line, a CRLF
ending loses its carriage return and a final newline does not start another line. The number in
the marker and the line numbers of read_output therefore agree.

Eviction (context/evict.py) replaces an old result with a stub that names the lines of the whole
output. Once the middle is cut, the marker is the one place that still knows that number, so
marker_line_count() reads it back.

This module is pure: it does no I/O and imports only the standard library and
longhaul.context.tokens, so the activities, the in-process loop and the tools can all use it.
"""

from __future__ import annotations

import math
import re

from longhaul.context.tokens import DEFAULT_CHARS_PER_TOKEN, estimate_tokens

__all__ = [
    "HEAD_LINES",
    "TAIL_LINES",
    "format_size",
    "marker_line_count",
    "shape_output",
    "split_lines",
]

# What the model keeps of a long output (plan section 7, LH-402).
HEAD_LINES = 60
TAIL_LINES = 60

_KB = 1024
_MB = 1024 * 1024


def split_lines(text: str) -> list[str]:
    """The lines of `text` the way rg, sed and tracebacks count them.

    Only "\\n" ends a line. str.splitlines also breaks at form feeds, vertical tabs, NEL and
    the Unicode separators, which would shift every line number after one. The "\\r" of a CRLF
    ending is dropped, so such a file reads like any other.
    """
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [ln.removesuffix("\r") for ln in lines]


def format_size(size: int) -> str:
    """A byte count as a person reads it: "900 bytes", "976.6 KB" or "3.0 MB"."""
    if size < _KB:
        return f"{size} byte" if size == 1 else f"{size} bytes"
    kilobytes = size / _KB
    # 1,048,575 bytes would read "1024.0 KB", which is 1.0 MB.
    if round(kilobytes, 1) < _KB:
        return f"{kilobytes:.1f} KB"
    return f"{size / _MB:.1f} MB"


# The start of a marker line. It has to open a line: output that only mentions a marker in the
# middle of a line is not one.
_MARKER_LINE = re.compile(r"^\[output truncated: (\d+) lines?, ", re.MULTILINE)


def marker_line_count(text: str) -> int | None:
    """The lines of the whole output, from the marker that shape_output() put in `text`.

    It is None for text that has no marker, which is output that was not cut.
    """
    found = _MARKER_LINE.search(text)
    return int(found.group(1)) if found else None


def _marker(lines: int, size: int, handle: str) -> str:
    unit = "line" if lines == 1 else "lines"
    return (
        f"[output truncated: {lines} {unit}, {format_size(size)}. Full text: handle {handle}. "
        "Use read_output to page or filter.]"
    )


def _fit(head: str, tail: str, room: int) -> tuple[str, str]:
    """Cut `head` and `tail` by characters until together they take at most `room`.

    Each side gets half the room, and a side that needs less hands the rest to the other.
    The head keeps its start and the tail keeps its end, because the start of an output has
    the first error and the end has the summary.
    """
    room = max(room, 0)
    if len(head) + len(tail) <= room:
        return head, tail
    head_room = min(len(head), room // 2)
    tail_room = min(len(tail), room - head_room)
    head_room = min(len(head), room - tail_room)
    return head[:head_room], tail[len(tail) - tail_room :]


def shape_output(full: str, max_tokens: int, handle: str) -> str:
    """The text the model reads for a tool output: all of it, or a head, a tail and a handle.

    Output of at most `max_tokens` tokens (estimate_tokens) is returned as it is. A longer one
    keeps its first HEAD_LINES and last TAIL_LINES lines with this marker between them:

        [output truncated: <lines> lines, <size>. Full text: handle <handle>. Use read_output
        to page or filter.]

    The result is at most `max_tokens` tokens by the same estimate. When the kept lines are
    too long for that, the head and the tail are cut by characters. When the cap is too small
    for the marker itself, the marker is returned alone: the handle matters more than the cap.
    """
    if estimate_tokens(full) <= max_tokens:
        return full
    lines = split_lines(full)
    marker = _marker(len(lines), len(full.encode("utf-8", errors="replace")), handle)
    # With 120 lines or fewer the tail starts where the head ends and nothing is left out by
    # line. Only the character cut below can then shorten the text.
    head = "\n".join(lines[:HEAD_LINES])
    tail = "\n".join(lines[max(HEAD_LINES, len(lines) - TAIL_LINES) :])
    # Characters that estimate_tokens still counts as `max_tokens`, less the marker and the two
    # line breaks around it.
    room = math.floor(max_tokens * DEFAULT_CHARS_PER_TOKEN) - len(marker) - 2
    head, tail = _fit(head, tail, room)
    return "\n".join(part for part in (head, marker, tail) if part)
