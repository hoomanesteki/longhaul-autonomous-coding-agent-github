"""The facts that tool_exec tells the stuck detector about one call (LH-505).

The detector (roles/stuck.py) lives in the workflow and cannot see the text of a result, which
the workflow history keeps only as a short preview. So the activity, which has the whole text,
reads the few facts the rules need and returns them with the result:

- the count of passing tests, from the closing line of a pytest run;
- for a command that failed, a sha1 of the command, its exit code and the end of what it
  printed, plus two short texts for the evidence: the command, and the error it ended with;
- for an edit, the lines it changed.

All of it is read from the text a tool made (the layout of tools/exec.py and tools/files.py),
because the registry returns only a ToolResult. tests/unit/test_signals.py builds the text with
the tools' own render functions, so a change to a layout fails a test here.

What the hash covers. The story's definition is the command, the exit code and the last 20 lines
of stderr. A crash prints its traceback to stderr, and that is what the hash reads. pytest prints
its report to stdout and leaves stderr empty or holding only a warning, and the same warning
above two different failures would hash alike. So when stderr names no error, the last 20 lines
of stdout are added. What changes from run to run without meaning anything is taken out before
hashing: run times, memory addresses and colour codes.

The planning phase is not watched, and tool_exec does not call this for it.
"""

from __future__ import annotations

import hashlib
import posixpath
import re

from longhaul.contracts import ToolCall, ToolResult
from longhaul.roles.stuck import CallSignal, Region, short, short_path
from longhaul.tools.exec import build_test_command, parse_pytest_summary, split_streams

RUN_COMMAND = "run_command"
RUN_TESTS = "run_tests"
EDIT_FILE = "edit_file"
WRITE_FILE = "write_file"

# The lines of a command's output that its hash covers.
TAIL_LINES = 20

_PASSED = re.compile(r"\b(\d+) passed\b")
# A word that ends in Error or Exception, such as KeyError, AssertionError or ValidationException.
_ERROR = re.compile(r"\b[A-Z][A-Za-z]*(?:Error|Exception)\b")
# pytest writes "path.py:42: KeyError" and Python writes 'File "path.py", line 42'.
_PLACES = (
    re.compile(r"^(?P<path>[^\s:]+\.\w+):(?P<line>\d+):"),
    re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)'),
)
_PYTEST_MARK = re.compile(r"^E\s+")
_WORKDIR = "/workspace/"
# What differs between two runs of the same failure: colours, run times such as "in 1.57s" and
# "(0:00:01)", and memory addresses.
_VOLATILE = (
    (re.compile(r"\x1b\[[0-9;]*[A-Za-z]"), ""),
    (re.compile(r"\(\d+:\d{2}:\d{2}\)"), "<time>"),
    (re.compile(r"\b\d+(?:\.\d+)?s\b"), "<time>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
_WROTE = re.compile(r"^wrote \d+ bytes to (?P<path>.+)$")


def call_signal(call: ToolCall, result: ToolResult, test_command: str | None) -> CallSignal | None:
    """The facts of one finished call, or None when it has none to give.

    `test_command` is the command that run_tests runs, which names the call in the evidence.
    """
    if call.name in (RUN_COMMAND, RUN_TESTS):
        fact = _ran(call, result, test_command)
    elif call.name in (EDIT_FILE, WRITE_FILE) and result.ok:
        region = _edited(call, result)
        fact = CallSignal(edited_regions=[region] if region else [])
    else:
        return None
    return fact if fact != CallSignal() else None


# ---------- commands ----------
def command_of(call: ToolCall, test_command: str | None) -> str | None:
    """The command line a run_command or run_tests call ran, as far as the call tells."""
    if call.name == RUN_COMMAND:
        command = call.input.get("command")
        return command if isinstance(command, str) else None
    if call.name != RUN_TESTS:
        return None
    target = call.input.get("target")
    target = target.strip() if isinstance(target, str) else ""
    if test_command is None:
        return f"{RUN_TESTS} {target}" if target else RUN_TESTS
    try:
        return build_test_command(test_command, target)
    except ValueError:  # the target has unbalanced quotes, and the tool refused it
        return f"{test_command} {target}"


def command_label(command: str) -> str:
    """The command as a person reads it: the program by its name, not by its path.

    A test command often starts with the full path of an interpreter, and that path alone can
    fill the evidence text, so the part that says what ran would be cut off. The hash still
    takes the whole command.
    """
    program, sep, rest = command.strip().partition(" ")
    return posixpath.basename(program) + sep + rest if "/" in program else command


def passed_count(output: str) -> int | None:
    """The tests that passed according to the closing line of a pytest run, or None.

    A run whose line has no "passed" part, such as "3 failed", passed none.
    """
    summary = parse_pytest_summary(output)
    if summary is None:
        return None
    found = _PASSED.search(summary)
    return int(found.group(1)) if found else 0


def _ran(call: ToolCall, result: ToolResult, test_command: str | None) -> CallSignal:
    command = command_of(call, test_command)
    code = result.exit_code
    tests = passed_count(result.output)
    # A call that never ran, such as run_tests without a test command, has no exit code. A
    # timeout has one that is not zero.
    if command is None or code in (None, 0):
        return CallSignal(tests_passed=tests)
    tail = printed_tail(result.output)
    return CallSignal(
        tests_passed=tests,
        failed_cmd_hash=failure_hash(command, code, tail),
        failed_command=short(command_label(command)),
        failed_error=_error_text(tail),
    )


def printed_tail(output: str) -> str:
    """The end of what a failed command printed, as far as it says what failed.

    That is the last TAIL_LINES lines of stderr when it names an error, and otherwise those of
    stderr and then those of stdout. See the module docstring for why.
    """
    stdout, stderr = split_streams(output)
    if _error_line(stderr) is not None:
        return _last_lines(stderr)
    return "\n".join(part for part in (_last_lines(stderr), _last_lines(stdout)) if part)


def _last_lines(text: str) -> str:
    return "\n".join(text.splitlines()[-TAIL_LINES:])


def failure_hash(command: str, exit_code: int, tail: str) -> str:
    for pattern, replacement in _VOLATILE:
        tail = pattern.sub(replacement, tail)
    return hashlib.sha1(
        f"{command}\n{exit_code}\n{tail}".encode(), usedforsecurity=False
    ).hexdigest()


def _error_line(text: str) -> str | None:
    return next((ln for ln in reversed(text.splitlines()) if _ERROR.search(ln)), None)


def _error_text(tail: str) -> str | None:
    """The error as "KeyError at inv.py:42" when the tail has one and a place, else its line.

    With no error in it, the last line stands for it: a build tool's "make: *** [lint] Failed"
    is as good a name for the failure as any.
    """
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return None
    error = _error_line("\n".join(lines))
    if error is None:
        return short(lines[-1])
    kind = _ERROR.search(error)
    place = _place(lines)
    if kind is not None and place is not None:
        return short(f"{kind.group(0)} at {place}")
    return short(_PYTEST_MARK.sub("", error))


def _place(lines: list[str]) -> str | None:
    """The last 'path:line' that the lines name, with the workspace folder left off."""
    for line in reversed(lines):
        for pattern in _PLACES:
            found = pattern.search(line)
            if found:
                path = found.group("path").removeprefix(_WORKDIR)
                return f"{path}:{found.group('line')}"
    return None


# ---------- edits ----------
def _edited(call: ToolCall, result: ToolResult) -> Region | None:
    if call.name == EDIT_FILE:
        return _diff_region(result.output)
    return _written_region(call, result.output)


def _diff_region(output: str) -> Region | None:
    """The lines that the unified diff of an edit_file result changed, in the new file.

    A removed line counts at the place it was, so that a deletion is a region of one line.
    """
    path: str | None = None
    touched: list[int] = []
    line = 0
    in_hunk = False
    for text in output.splitlines():
        hunk = _HUNK.match(text)
        if hunk:
            line, in_hunk = int(hunk.group("start")), True
        elif not in_hunk:
            if text.startswith("+++ b/"):
                path = text.removeprefix("+++ b/")
        elif text.startswith("+"):
            touched.append(line)
            line += 1
        elif text.startswith("-"):
            touched.append(line)
        elif text.startswith(" "):
            line += 1
    if path is None or not touched:
        return None
    return short_path(path), min(touched), max(touched)


def _written_region(call: ToolCall, output: str) -> Region | None:
    """The whole file, for a write_file call: it replaced every line."""
    found = _WROTE.match(output.strip())
    content = call.input.get("content")
    if found is None or not isinstance(content, str):
        return None
    lines = content.count("\n") + (0 if not content or content.endswith("\n") else 1)
    return short_path(found.group("path")), 1, max(lines, 1)
