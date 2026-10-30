"""File tools: list_dir, read_file, edit_file, write_file."""

from __future__ import annotations

import difflib
import shlex
from typing import Any

from longhaul.contracts import Sandbox, ToolContext, ToolResult, ToolSpec
from longhaul.tools import ToolError
from longhaul.tools.paths import Resolved, resolve

MAX_READ_LINES = 800
MAX_LIST_ENTRIES = 400
MAX_DIFF_LINES = 80
IGNORED_DIRS = (".git", "__pycache__", ".venv", "node_modules")


def ok(output: str) -> ToolResult:
    return ToolResult(call_id="", ok=True, output=output)


def fail(output: str) -> ToolResult:
    return ToolResult(call_id="", ok=False, output=output)


def format_diff(path: str, old: str, new: str) -> str:
    lines = list(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
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


def number_lines(text: str, start: int, end: int | None) -> str:
    lines = text.splitlines()
    if start > max(len(lines), 1) or (not lines and start > 1):
        return f"start_line {start} is beyond the end of the file ({len(lines)} lines)"
    last = len(lines) if end is None else min(end, len(lines))
    capped = min(last, start + MAX_READ_LINES - 1)
    out = [f"{n:>6}\t{lines[n - 1]}" for n in range(start, capped + 1)]
    if capped < last:
        out.append(
            f"[showing lines {start}-{capped} of {len(lines)}; "
            f"call again with start_line={capped + 1} to continue]"
        )
    return "\n".join(out)


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


def _path_prop(desc: str) -> dict[str, Any]:
    return {"type": "string", "description": desc}


async def load_text(sandbox: Sandbox, raw: str) -> tuple[Resolved, str]:
    """Resolve `raw`, check that it is a regular file and return its path and text."""
    found = await resolve(sandbox, raw)
    found.require_file()
    data = await sandbox.read_file(found.rel)
    try:
        return found, data.decode("utf-8")
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
        try:
            _, text = await load_text(sandbox, args["path"])
        except (ToolError, FileNotFoundError) as exc:
            return fail(str(exc))
        return ok(number_lines(text, int(args.get("start_line", 1)), args.get("end_line")))


class EditFile:
    spec = ToolSpec(
        name="edit_file",
        description=(
            "Replace one exact piece of text in a file. old_str must match exactly once, "
            "so include enough surrounding lines."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": _path_prop("File relative to /workspace."),
                "old_str": {"type": "string", "minLength": 1},
                "new_str": {"type": "string"},
            },
            "required": ["path", "old_str", "new_str"],
            "additionalProperties": False,
        },
    )
    mutates = True

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            found, old = await load_text(sandbox, args["path"])
        except (ToolError, FileNotFoundError) as exc:
            return fail(str(exc))
        count = old.count(args["old_str"])
        if count != 1:
            hint = "add more context" if count else "check the exact text with read_file"
            return fail(
                f"found {count} matches for old_str in {found.requested}; need exactly 1 ({hint})"
            )
        new = old.replace(args["old_str"], args["new_str"], 1)
        await sandbox.write_file(found.rel, new.encode("utf-8"))
        return ok(format_diff(found.rel, old, new))


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
        except ToolError as exc:
            return fail(str(exc))
        data = args["content"].encode("utf-8")
        await sandbox.write_file(found.rel, data)
        return ok(f"wrote {len(data)} bytes to {found.rel}")
