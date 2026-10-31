"""File tools: list_dir, read_file, edit_file, write_file."""

from __future__ import annotations

import difflib
import re
import shlex
from dataclasses import dataclass
from typing import Any

from longhaul.contracts import Sandbox, ToolContext, ToolResult, ToolSpec
from longhaul.tools import ToolError, to_utf8
from longhaul.tools.paths import Resolved, resolve

MAX_READ_LINES = 800
MAX_LIST_ENTRIES = 400
MAX_DIFF_LINES = 80
IGNORED_DIRS = (".git", "__pycache__", ".venv", "node_modules")

# A page of read_file has to fit in ToolContext.output_max_tokens. Anything longer has
# its middle cut out later (LH-402), and the cut lines are ones the footer promised.
# The ratio is the plan's token estimate: characters divided by 3.5.
CHARS_PER_TOKEN = 3.5
DEFAULT_READ_CHARS = int(4000 * CHARS_PER_TOKEN)  # the default output_max_tokens
FOOTER_RESERVE = 120  # room left in a page for its "call again" footer
MIN_PAGE_CHARS = 200  # however small the budget, a page shows one line, cut if it must
CUT_MARKER_RESERVE = 40  # room for "[line cut: N more characters]" after a cut line

CRLF_NOTE = "note: the file uses CRLF line endings, so line breaks in old_str and new_str are CRLF."
NUMBERED_OLD_STR = (
    "old_str starts each line with the line numbers that read_file prints. "
    "Those numbers are not part of the file; send the text without them."
)

_DIFF_LINE = re.compile(r"[^\n]*\n|[^\n]+")
_NUMBERED_LINE = re.compile(r"\s*\d+\t")


def ok(output: str) -> ToolResult:
    return ToolResult(call_id="", ok=True, output=output)


def fail(output: str) -> ToolResult:
    return ToolResult(call_id="", ok=False, output=output)


def split_lines(text: str) -> list[str]:
    """The lines of `text` the way rg, sed and tracebacks count them.

    Only "\\n" ends a line. str.splitlines also breaks at form feeds, vertical tabs,
    NEL and the Unicode separators, which would shift every line number after one.
    The "\\r" of a CRLF ending is dropped, so such a file reads like any other.
    """
    if not text:
        return []
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [ln.removesuffix("\r") for ln in lines]


def format_diff(path: str, old: str, new: str) -> str:
    # Lines are cut at "\n" only, so the hunk numbers match read_file and rg. CRLF
    # endings are left out of the picture, as they are in read_file.
    lines = list(
        difflib.unified_diff(
            _DIFF_LINE.findall(old.replace("\r\n", "\n")),
            _DIFF_LINE.findall(new.replace("\r\n", "\n")),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=2,
        )
    )
    shown = [ln if ln.endswith("\n") else ln + "\n" for ln in lines[:MAX_DIFF_LINES]]
    out = "".join(shown).rstrip("\n")
    if len(lines) > MAX_DIFF_LINES:
        out += f"\n... diff truncated ({len(lines) - MAX_DIFF_LINES} more lines)"
    return out


def _numbered(n: int, line: str, room: int) -> str:
    prefix = f"{n:>6}\t"
    if len(prefix) + len(line) <= room:
        return prefix + line
    space = max(room - len(prefix) - CUT_MARKER_RESERVE, 1)
    return f"{prefix}{line[:space]} [line cut: {len(line) - space} more characters]"


def number_lines(
    text: str, start: int, end: int | None, max_chars: int = DEFAULT_READ_CHARS
) -> str:
    """Lines `start` to `end` with their numbers, as many as fit in `max_chars`.

    A page also ends after MAX_READ_LINES lines. When it ends early, a footer names
    the line to go on from. Raises ToolError for a range that cannot be read.
    """
    lines = split_lines(text)
    total = len(lines)
    if end is not None and end < start:
        raise ToolError(f"end_line {end} is before start_line {start}")
    if not lines:
        if start > 1:
            raise ToolError(f"start_line {start} is beyond the end of the file (it is empty)")
        return "(empty file)"
    if start > total:
        size = "1 line" if total == 1 else f"{total} lines"
        raise ToolError(f"start_line {start} is beyond the end of the file ({size})")
    last = total if end is None else min(end, total)
    room = max(max_chars - FOOTER_RESERVE, MIN_PAGE_CHARS)
    rows: list[str] = []
    used = 0
    n = start
    while n <= last and n - start < MAX_READ_LINES:
        row = _numbered(n, lines[n - 1], room)
        if rows and used + len(row) + 1 > room:
            break
        rows.append(row)
        used += len(row) + 1
        n += 1
    if n <= last:
        rows.append(
            f"[showing lines {start}-{n - 1} of {total}; "
            f"call again with start_line={n} to continue]"
        )
    return "\n".join(rows)


def render_tree(paths: list[str]) -> str:
    """Indented listing of sorted relative paths. Directories end with '/'."""
    out: list[str] = []
    for p in paths[:MAX_LIST_ENTRIES]:
        trimmed = p.rstrip("/")
        depth = trimmed.count("/")
        name = trimmed.rsplit("/", 1)[-1] + ("/" if p.endswith("/") else "")
        out.append("  " * depth + name)
    if len(paths) > MAX_LIST_ENTRIES:
        out.append(f"... {len(paths) - MAX_LIST_ENTRIES} more entries not shown")
    return "\n".join(out) if out else "(empty)"


def find_all(text: str, needle: str) -> list[int]:
    """Where `needle` starts in `text`, overlapping matches included.

    str.count skips overlaps, so it calls "aa" unique in "aaa" although it fits at two
    places. An edit anchored on such text has to be refused as ambiguous.
    """
    starts: list[int] = []
    at = text.find(needle)
    while at != -1:
        starts.append(at)
        at = text.find(needle, at + 1)
    return starts


def to_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def uses_crlf(text: str) -> bool:
    """True when most of the line endings are CRLF."""
    crlf = text.count("\r\n")
    return crlf > text.count("\n") - crlf


def looks_numbered(text: str) -> bool:
    """True when every non-blank line starts with a number and a tab, as read_file prints."""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    return bool(lines) and all(_NUMBERED_LINE.match(ln) for ln in lines)


@dataclass(frozen=True)
class Edit:
    start: int
    matched: str  # the text to replace, spelled as the file has it
    replacement: str
    note: str | None = None

    def apply(self, text: str) -> str:
        return text[: self.start] + self.replacement + text[self.start + len(self.matched) :]


def plan_edit(path: str, text: str, old_str: str, new_str: str) -> Edit:
    """Find the one place `old_str` matches and what goes there. Raises ToolError otherwise.

    read_file shows a CRLF file without its carriage returns, so a multi-line old_str
    copied from there has none. When it matches nowhere as given, it is tried with CRLF
    endings, and the new text is written with them as well.
    """
    hits = find_all(text, old_str)
    matched = old_str
    if not hits and "\n" in old_str and "\r\n" in text:
        matched = to_crlf(old_str)
        hits = find_all(text, matched)
    if len(hits) != 1:
        if not hits and looks_numbered(old_str):
            raise ToolError(NUMBERED_OLD_STR)
        hint = "add more context" if hits else "check the exact text with read_file"
        raise ToolError(f"found {len(hits)} matches for old_str in {path}; need exactly 1 ({hint})")
    replacement = to_crlf(new_str) if matched != old_str or uses_crlf(text) else new_str
    changed = matched != old_str or replacement != new_str
    return Edit(hits[0], matched, replacement, CRLF_NOTE if changed else None)


def _path_prop(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


async def load_text(sandbox: Sandbox, raw: str) -> tuple[Resolved, str]:
    """Resolve `raw`, check that it is a regular file and return its path and text."""
    found = await resolve(sandbox, raw)
    found.require_file()
    return found, await read_text(sandbox, found)


async def read_text(sandbox: Sandbox, found: Resolved) -> str:
    data = await sandbox.read_file(found.rel)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ToolError(f"not a UTF-8 text file: {found.requested}") from None


class ListDir:
    spec = ToolSpec(
        name="list_dir",
        description="List files and folders under a directory. Use it to find your way around.",
        input_schema={
            "type": "object",
            "properties": {
                "path": _path_prop("Directory relative to /workspace. Default '.'."),
                "depth": {"type": "integer", "minimum": 1, "maximum": 6, "default": 2},
            },
            "additionalProperties": False,
        },
    )
    mutates = False

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            found = await resolve(sandbox, args.get("path", "."))
            found.require_dir()
        except (ToolError, FileNotFoundError) as exc:
            return fail(str(exc))
        depth = int(args.get("depth", 2))
        prune = " -o ".join(f"-name {shlex.quote(n)}" for n in IGNORED_DIRS)
        # find reads a first argument that starts with a dash as an option, and it has no
        # `--` to stop that. So go into the directory and always search ".".
        cmd = (
            f"cd -- {shlex.quote(found.rel)} && "
            f"find . -mindepth 1 -maxdepth {depth} "
            f"\\( {prune} \\) -prune -o "
            "\\( -type d -exec printf '%s/\\n' {} + -o -print \\)"
        )
        res = await sandbox.exec(cmd, timeout_s=30)
        if res.exit_code != 0:
            return fail(f"list_dir failed: {res.stderr.strip()}")
        paths = sorted(ln.removeprefix("./") for ln in res.stdout.split("\n") if ln)
        return ok(render_tree(paths))


class ReadFile:
    spec = ToolSpec(
        name="read_file",
        description="Read a file with line numbers. Use start_line and end_line for big files.",
        input_schema={
            "type": "object",
            "properties": {
                "path": _path_prop("File relative to /workspace."),
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    mutates = False

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # The schema accepts 2.0 for an integer, and some servers send it.
        end = args.get("end_line")
        try:
            _, text = await load_text(sandbox, args["path"])
            page = number_lines(
                text,
                int(args.get("start_line", 1)),
                None if end is None else int(end),
                int(ctx.output_max_tokens * CHARS_PER_TOKEN),
            )
        except (ToolError, FileNotFoundError) as exc:
            return fail(str(exc))
        return ok(page)


class EditFile:
    spec = ToolSpec(
        name="edit_file",
        description=(
            "Replace one exact piece of text in a file. old_str must match exactly once, "
            "so add surrounding lines, and must not contain read_file's line numbers."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": _path_prop("File relative to /workspace."),
                "old_str": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The text as the file has it, without read_file's line numbers.",
                },
                "new_str": {"type": "string"},
            },
            "required": ["path", "old_str", "new_str"],
            "additionalProperties": False,
        },
    )
    mutates = True

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        old_str, new_str = args["old_str"], args["new_str"]
        try:
            found = await resolve(sandbox, args["path"])
            found.require_file()
            if old_str == new_str:
                raise ToolError("old_str and new_str are identical, so the file would not change")
            old = await read_text(sandbox, found)
            edit = plan_edit(found.requested, old, old_str, new_str)
            new = edit.apply(old)
            data = to_utf8(new)
        except (ToolError, FileNotFoundError) as exc:
            return fail(str(exc))
        await sandbox.write_file(found.rel, data)
        diff = format_diff(found.rel, old, new)
        return ok(diff if edit.note is None else f"{diff}\n{edit.note}")


class WriteFile:
    spec = ToolSpec(
        name="write_file",
        description="Create a file or overwrite it. Prefer edit_file for small changes.",
        input_schema={
            "type": "object",
            "properties": {
                "path": _path_prop("File relative to /workspace."),
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )
    mutates = True

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            found = await resolve(sandbox, args["path"])
            found.require_writable()
            data = to_utf8(args["content"])
        except ToolError as exc:
            return fail(str(exc))
        await sandbox.write_file(found.rel, data)
        return ok(f"wrote {len(data)} bytes to {found.rel}")
