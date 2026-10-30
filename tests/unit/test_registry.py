from collections.abc import AsyncIterator
from typing import Any

import pytest

from longhaul.contracts import Sandbox, ToolCall, ToolContext, ToolResult, ToolSpec
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import ToolRegistry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")


class EchoTool:
    spec = ToolSpec(
        name="echo",
        description="Echo the text back.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    )
    mutates = False

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id="", ok=True, output=args["text"])


class BoomTool(EchoTool):
    spec = ToolSpec(name="boom", description="Always fails.", input_schema={"type": "object"})

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise RuntimeError("kaput")


@pytest.fixture
async def sbx() -> AsyncIterator[FakeSandbox]:
    s = FakeSandbox()
    yield s
    await s.destroy()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(EchoTool())
    reg.register(BoomTool())
    return reg


async def test_valid_call_runs(registry: ToolRegistry, sbx: FakeSandbox) -> None:
    r = await registry.run(ToolCall(id="c1", name="echo", input={"text": "hi"}), sbx, CTX)
    assert r.ok and r.output == "hi" and r.call_id == "c1"
    assert r.duration_ms >= 0


async def test_schema_errors_are_results(registry: ToolRegistry, sbx: FakeSandbox) -> None:
    r = await registry.run(ToolCall(id="c2", name="echo", input={}), sbx, CTX)
    assert not r.ok
    assert r.call_id == "c2"
    assert "'text' is a required property" in r.output
    r = await registry.run(ToolCall(id="c3", name="echo", input={"text": 5}), sbx, CTX)
    assert not r.ok and "5 is not of type 'string'" in r.output


async def test_unknown_tool_is_result(registry: ToolRegistry, sbx: FakeSandbox) -> None:
    r = await registry.run(ToolCall(id="c4", name="nope", input={}), sbx, CTX)
    assert not r.ok and "unknown tool: nope" in r.output


async def test_tool_exception_is_result(registry: ToolRegistry, sbx: FakeSandbox) -> None:
    r = await registry.run(ToolCall(id="c5", name="boom", input={}), sbx, CTX)
    assert not r.ok
    assert "RuntimeError" in r.output and "kaput" in r.output


def test_duplicate_register_raises(registry: ToolRegistry) -> None:
    with pytest.raises(ValueError, match="echo"):
        registry.register(EchoTool())


def test_specs_keep_order_and_filter(registry: ToolRegistry) -> None:
    assert [s.name for s in registry.specs()] == ["echo", "boom"]
    assert [s.name for s in registry.specs(["boom"])] == ["boom"]
    with pytest.raises(KeyError):
        registry.specs(["missing"])
    assert registry.get("echo") is not None
    assert registry.get("missing") is None
