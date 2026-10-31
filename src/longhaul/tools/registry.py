"""Tool registry: lookup, schema validation and uniform error results."""

from __future__ import annotations

import time
from typing import Any

from jsonschema import Draft202012Validator

from longhaul.contracts import Sandbox, Tool, ToolCall, ToolContext, ToolResult, ToolSpec
from longhaul.tools import ToolError

# Errors that mean the model's input was wrong. They become results the model reads
# and can act on. PathError is a ToolError. The built-in errors come from the sandbox
# when a path is missing, is the wrong kind, or cannot be written (DockerSandbox raises
# the last three on type conflicts).
#
# Everything else propagates. A docker NotFound or APIError, a dropped connection or a
# bug is not the model's doing, and a result would hide it: the caller could neither
# recreate a lost container nor let Temporal retry the activity.
BAD_INPUT: tuple[type[Exception], ...] = (
    ToolError,
    FileNotFoundError,
    IsADirectoryError,
    NotADirectoryError,
    FileExistsError,
    PermissionError,
    UnicodeDecodeError,
)

# The OpenAI-compatible provider puts the text of tool arguments it could not parse
# as a JSON object under this key: {"_raw": text}.
RAW_ARGUMENTS = "_raw"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._validators: dict[str, Draft202012Validator] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        # A schema that cannot be checked against would fail on the first call instead.
        Draft202012Validator.check_schema(tool.spec.input_schema)
        self._tools[name] = tool
        self._validators[name] = Draft202012Validator(tool.spec.input_schema)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        if names is None:
            return [t.spec for t in self._tools.values()]
        return [self._tools[n].spec for n in names]

    async def run(self, call: ToolCall, sandbox: Sandbox, ctx: ToolContext) -> ToolResult:
        start = time.monotonic()
        result = await self._run(call, sandbox, ctx)
        elapsed = int((time.monotonic() - start) * 1000)
        return result.model_copy(update={"call_id": call.id, "duration_ms": elapsed})

    async def _run(self, call: ToolCall, sandbox: Sandbox, ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return _fail(f"unknown tool: {call.name}")
        args = call.input
        if set(args) == {RAW_ARGUMENTS}:
            if not _is_blank(args[RAW_ARGUMENTS]):
                return _fail(_unparseable(call.name))
            args = {}  # an empty string is how some servers send "no arguments"
        error = next(iter(self._validators[call.name].iter_errors(args)), None)
        if error is not None:
            where = "/".join(str(p) for p in error.absolute_path)
            prefix = f"invalid input for {call.name}" + (f" at {where}" if where else "")
            return _fail(f"{prefix}: {error.message}")
        try:
            return await tool.run(sandbox, args, ctx)
        except BAD_INPUT as exc:
            # A ToolError carries a message written for the model. The built-in errors
            # often have only a path as text, so they name their type.
            return _fail(str(exc) if isinstance(exc, ToolError) else f"{type(exc).__name__}: {exc}")


def _is_blank(raw: Any) -> bool:
    return isinstance(raw, str) and not raw.strip()


def _unparseable(name: str) -> str:
    return (
        f"the arguments for {name} were not a complete JSON object (the reply may have been "
        "cut off at max_tokens); send one shorter call with complete JSON"
    )


def _fail(message: str) -> ToolResult:
    return ToolResult(call_id="", ok=False, output=message)


def default_registry(
    *, test_command: str | None = None, max_timeout_s: int | None = None
) -> ToolRegistry:
    """The tools every role starts from.

    `test_command` is RepoSettings.test_command. Without it run_tests answers
    that no test command is set. `max_timeout_s` is sandbox.command_timeout_s
    and falls back to 600 seconds. Callers that pass no arguments keep working.
    """
    from longhaul.tools.control import Finish
    from longhaul.tools.exec import DEFAULT_MAX_TIMEOUT_S, RunCommand, RunTests
    from longhaul.tools.files import EditFile, ListDir, ReadFile, WriteFile
    from longhaul.tools.git import GitDiff
    from longhaul.tools.search import Search

    limit = DEFAULT_MAX_TIMEOUT_S if max_timeout_s is None else max_timeout_s
    reg = ToolRegistry()
    for tool in (
        ListDir(),
        ReadFile(),
        EditFile(),
        WriteFile(),
        Search(),
        RunCommand(limit),
        RunTests(test_command, limit),
        GitDiff(),
        Finish(),
    ):
        reg.register(tool)
    return reg
