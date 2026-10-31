"""Code search through ripgrep inside the sandbox."""

from __future__ import annotations

import base64
import json
import shlex
from typing import Any

from longhaul.contracts import Sandbox, ToolContext, ToolResult, ToolSpec
from longhaul.tools import ToolError, to_utf8
from longhaul.tools.files import fail, ok
from longhaul.tools.paths import resolve

MAX_LINE_CHARS = 300
SEARCH_TIMEOUT_S = 60
# How many of rg's complaints the note shows, and how long the note may get.
MAX_NOTED_ERRORS = 3
MAX_NOTE_CHARS = 300

Match = tuple[str, int, str]


def _text(field: dict[str, str]) -> str:
    """The text of a path or a line in rg's JSON.

    rg sends {"text": ...} and, for bytes that are not valid UTF-8, {"bytes": base64}.
    """
    if "text" in field:
        return field["text"]
    return base64.b64decode(field.get("bytes", "")).decode("utf-8", errors="replace")


def parse_rg_json(stdout: str) -> list[Match]:
    matches: list[Match] = []
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        text = _text(data["lines"]).rstrip("\n")
        matches.append((_text(data["path"]), data["line_number"], text[:MAX_LINE_CHARS]))
    return matches


def format_matches(matches: list[Match], max_results: int) -> str:
    lines = [f"{p}:{n}: {t}" for p, n, t in matches[:max_results]]
    if len(matches) > max_results:
        lines.append(f"... {len(matches) - max_results} more matches not shown")
    return "\n".join(lines)


def unread_note(stderr: str) -> str:
    """Tell the model that rg skipped something, with the first few reasons it gave."""
    problems = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    if not problems:
        return "note: rg reported an error, so the matches may be incomplete"
    shown = "; ".join(problems[:MAX_NOTED_ERRORS])
    if len(problems) > MAX_NOTED_ERRORS:
        shown += f"; and {len(problems) - MAX_NOTED_ERRORS} more"
    return (
        "note: some files could not be searched, so matches in them are missing "
        f"({shown[:MAX_NOTE_CHARS]})"
    )


def rg_command(args: dict[str, Any], path: str) -> str:
    # The pattern follows -e and the path follows --, so neither can be read as an option.
    cmd = ["rg", "--json", "-e", args["pattern"]]
    if args.get("glob"):
        cmd += ["-g", args["glob"]]
    cmd += ["--", path]
    return " ".join(shlex.quote(c) for c in cmd)


class Search:
    spec = ToolSpec(
        name="search",
        description="Search file contents with a regex. Use it to find where a symbol is used.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1},
                "path": {"type": "string", "default": "."},
                "glob": {"type": "string", "description": "Only files matching, e.g. '*.py'."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 200},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    )
    mutates = False

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            to_utf8(args["pattern"], "the pattern")
            to_utf8(args.get("glob", ""), "the glob")
            found = await resolve(sandbox, args.get("path", "."))
            found.require_exists()
        except (ToolError, FileNotFoundError) as exc:
            return fail(str(exc))
        res = await sandbox.exec(rg_command(args, found.rel), timeout_s=SEARCH_TIMEOUT_S)
        if res.timed_out:
            return fail(f"search timed out after {SEARCH_TIMEOUT_S} s; use a narrower path or glob")
        # rg searches files on several threads and prints each one when its thread is done.
        # Sorting first gives the same text for the same call, and the cut at max_results
        # keeps the same matches.
        matches = sorted(parse_rg_json(res.stdout), key=lambda m: (m[0], m[1]))
        # Exit 1 means no match. Exit 2 means some error, even when rg printed matches
        # before it, as it does for a file it cannot read: keep what it found.
        if res.exit_code not in (0, 1, 2) or (res.exit_code == 2 and not matches):
            return fail(f"rg failed: {res.stderr.strip()}")
        out = format_matches(matches, int(args.get("max_results", 200))) or "no matches"
        if res.exit_code == 2:
            out += "\n" + unread_note(res.stderr)
        return ok(out)
