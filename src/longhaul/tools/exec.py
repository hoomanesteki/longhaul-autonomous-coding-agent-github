"""Exec tools: run_command and run_tests.

Output handling is simple on purpose. Each stream is cut to a head and a tail
with a marker between them. Phase 4 (LH-402) replaces this with output handles.

Only a timeout is a tool error (ok=False). A command that exits non-zero still
ran, so a failing test run does not count towards the consecutive tool-error
rule. The exit code travels in ToolResult.exit_code and in the first output line.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from longhaul.contracts import ExecResult, Sandbox, ToolContext, ToolResult, ToolSpec
from longhaul.tools import ToolError, to_utf8
from longhaul.tools.files import fail

DEFAULT_TIMEOUT_S = 300
# Same value as sandbox.command_timeout_s in config/longhaul.example.yaml. Code
# that has loaded the config passes its own value to default_registry.
DEFAULT_MAX_TIMEOUT_S = 600

HEAD_CHARS = 8_000
TAIL_CHARS = 12_000
MAX_STREAM_CHARS = HEAD_CHARS + TAIL_CHARS

COMMAND_TIMEOUT_HINT = "Run something narrower, or raise timeout_s (max {max_s})."
TESTS_TIMEOUT_HINT = "Run one file or test id with target, or look for a hanging test."

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# The words pytest prints after a count in its closing line.
_KINDS = (
    "failed",
    "passed",
    "skipped",
    "deselected",
    "xfailed",
    "xpassed",
    "warnings?",
    "errors?",
    "reruns?",  # pytest-rerunfailures
    "subtests (?:passed|failed|skipped)",
)
_COUNT = rf"\d+ (?:{'|'.join(_KINDS)})"
_SUMMARY = re.compile(
    rf"^[= ]*(?P<body>no tests ran|{_COUNT}(?:, {_COUNT})*)"
    r" in \d+(?:\.\d+)?s(?: \([^)]*\))?[= ]*$"
)


def cap_text(text: str) -> str:
    """Keep the first HEAD_CHARS and the last TAIL_CHARS of long text.

    The marker between them says how many characters were cut. It is extra: the
    kept text is exactly MAX_STREAM_CHARS.
    """
    if len(text) <= MAX_STREAM_CHARS:
        return text
    cut = len(text) - MAX_STREAM_CHARS
    return f"{text[:HEAD_CHARS]}\n[output truncated: {cut} characters cut]\n{text[-TAIL_CHARS:]}"


def clamp_timeout(requested: float | None, maximum: int) -> int:
    """The timeout to use: the default when none was asked for, never above `maximum`."""
    wanted = DEFAULT_TIMEOUT_S if requested is None else int(requested)
    return max(1, min(wanted, maximum))


def build_test_command(base: str, target: str | None) -> str:
    """Append the target to the configured test command.

    The target is split like a shell would and every word is quoted again, so a
    path or a `-k` expression works and shell syntax in it stays plain text.
    Raises ValueError for unbalanced quotes.
    """
    if target is None or not target.strip():
        return base
    words = " ".join(shlex.quote(word) for word in shlex.split(target))
    return f"{base} {words}"


def parse_pytest_summary(text: str) -> str | None:
    """Return the counts from pytest's closing line, such as "1 failed, 2 passed".

    A line only counts when it ends with the run time, so a collect-only listing
    or an "Interrupted" banner is not mistaken for a summary. The last match wins.
    """
    for line in reversed(_ANSI.sub("", text).splitlines()):
        match = _SUMMARY.match(line.strip())
        if match:
            return match.group("body")
    return None


def _summary_of(res: ExecResult) -> str | None:
    return parse_pytest_summary(res.stdout) or parse_pytest_summary(res.stderr)


def _stream_sections(res: ExecResult) -> list[str]:
    sections: list[str] = []
    for label, text in (("stdout", res.stdout), ("stderr", res.stderr)):
        body = text.rstrip("\n")
        if body:
            sections += [f"{label}:", cap_text(body)]
    return sections


def render_exit(res: ExecResult, summary: str | None = None) -> str:
    lines = [f"exit code: {res.exit_code}"]
    if summary:
        lines.append(f"summary: {summary}")
    lines += _stream_sections(res) or ["(no output)"]
    return "\n".join(lines)


def render_timeout(res: ExecResult, seconds: int, hint: str) -> str:
    # Whatever was printed before the kill is kept, because it often shows where it hung.
    lines = [f"timed out after {seconds} s and was killed. {hint}", *_stream_sections(res)]
    return "\n".join(lines)


def to_result(
    res: ExecResult, timeout_s: int, *, hint: str, summary: str | None = None
) -> ToolResult:
    ok = not res.timed_out
    output = render_exit(res, summary) if ok else render_timeout(res, timeout_s, hint)
    return ToolResult(
        call_id="", ok=ok, output=output, exit_code=res.exit_code, duration_ms=res.duration_ms
    )


class RunCommand:
    # The tool cannot tell in advance whether a command changes files. The tool
    # activity checks for a dirty tree after exec tools instead (LH-206).
    mutates = False

    def __init__(self, max_timeout_s: int = DEFAULT_MAX_TIMEOUT_S) -> None:
        self.max_timeout_s = max_timeout_s
        self.spec = ToolSpec(
            name="run_command",
            description=(
                "Run a shell command in /workspace and get its exit code and output. "
                "Use it for anything the other tools do not cover."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1},
                    "timeout_s": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            f"Seconds before the command is killed. Default {DEFAULT_TIMEOUT_S}, "
                            f"at most {max_timeout_s}."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        )

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            to_utf8(args["command"], "the command")
        except ToolError as exc:
            return fail(str(exc))
        timeout_s = clamp_timeout(args.get("timeout_s"), self.max_timeout_s)
        res = await sandbox.exec(args["command"], timeout_s=timeout_s)
        hint = COMMAND_TIMEOUT_HINT.format(max_s=self.max_timeout_s)
        return to_result(res, timeout_s, hint=hint)


class RunTests:
    mutates = False  # see RunCommand

    spec = ToolSpec(
        name="run_tests",
        description=(
            "Run the repo's test command, or only one file or test id. Use it after every change."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Test file, directory or id, such as tests/test_calc.py::test_add."
                    ),
                },
            },
            "additionalProperties": False,
        },
    )

    def __init__(
        self, test_command: str | None, max_timeout_s: int = DEFAULT_MAX_TIMEOUT_S
    ) -> None:
        self.test_command = test_command
        self.max_timeout_s = max_timeout_s

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self.test_command:
            return fail("no test command is configured for this repo; use run_command instead")
        try:
            to_utf8(args.get("target") or "", "the target")
            command = build_test_command(self.test_command, args.get("target"))
        except ToolError as exc:
            return fail(str(exc))
        except ValueError as exc:
            return fail(f"could not read target: {exc}")
        # The model cannot pick a timeout here, and test suites are the commands
        # most likely to run long, so they get the full configured allowance.
        res = await sandbox.exec(command, timeout_s=self.max_timeout_s)
        summary = None if res.timed_out else _summary_of(res)
        return to_result(res, self.max_timeout_s, hint=TESTS_TIMEOUT_HINT, summary=summary)
