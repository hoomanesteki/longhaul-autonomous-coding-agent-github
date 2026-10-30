"""Tool registry: lookup, schema validation and uniform error results."""

from __future__ import annotations

import time

from jsonschema import Draft202012Validator

from longhaul.contracts import Sandbox, Tool, ToolCall, ToolContext, ToolResult, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._validators: dict[str, Draft202012Validator] = {}

    def register(self, tool: Tool) -> None:
        name = tool.spec.name
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
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
        error = next(iter(self._validators[call.name].iter_errors(call.input)), None)
        if error is not None:
            where = "/".join(str(p) for p in error.absolute_path)
            prefix = f"invalid input for {call.name}" + (f" at {where}" if where else "")
            return _fail(f"{prefix}: {error.message}")
        try:
            return await tool.run(sandbox, call.input, ctx)
        except Exception as exc:  # noqa: BLE001 - any tool failure becomes a result
            return _fail(f"{type(exc).__name__}: {exc}")


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
