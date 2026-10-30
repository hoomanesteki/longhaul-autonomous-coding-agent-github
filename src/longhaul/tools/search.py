"""Code search through ripgrep inside the sandbox."""

from __future__ import annotations

import json
import shlex
from typing import Any

from longhaul.contracts import Sandbox, ToolContext, ToolResult, ToolSpec
from longhaul.tools.paths import PathError, safe_path

MAX_LINE_CHARS = 300

Match = tuple[str, int, str]


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
        text = data["lines"].get("text", "").rstrip("\n")
        matches.append((data["path"]["text"], data["line_number"], text[:MAX_LINE_CHARS]))
    return matches


def format_matches(matches: list[Match], max_results: int) -> str:
    lines = [f"{p}:{n}: {t}" for p, n, t in matches[:max_results]]
    if len(matches) > max_results:
        lines.append(f"... {len(matches) - max_results} more matches not shown")
    return "\n".join(lines)


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
            rel = await safe_path(sandbox, args.get("path", "."), must_exist=True)
        except (PathError, FileNotFoundError) as exc:
            return ToolResult(call_id="", ok=False, output=str(exc))
        cmd = ["rg", "--json", "-e", args["pattern"]]
        if args.get("glob"):
            cmd += ["-g", args["glob"]]
        cmd += ["--", rel]
        res = await sandbox.exec(" ".join(shlex.quote(c) for c in cmd), timeout_s=60)
        if res.exit_code == 1:
            return ToolResult(call_id="", ok=True, output="no matches")
        if res.exit_code != 0:
            return ToolResult(call_id="", ok=False, output=f"rg failed: {res.stderr.strip()}")
        out = format_matches(parse_rg_json(res.stdout), int(args.get("max_results", 200)))
        return ToolResult(call_id="", ok=True, output=out or "no matches")
