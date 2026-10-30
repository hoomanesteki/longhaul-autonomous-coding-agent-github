import pytest

from longhaul.contracts import ExecResult, ToolCall, ToolContext
from longhaul.tools.control import CONTROL_TOOL_NAMES, FINISH, Finish, is_control_tool
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

CTX = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")


class UntouchableSandbox:
    """Fails the test on any use. A control tool must never reach the sandbox."""

    id = "untouchable"
    workdir = "/workspace"

    async def exec(
        self, cmd: str, *, timeout_s: int, env: dict[str, str] | None = None
    ) -> ExecResult:
        raise AssertionError(f"control tool ran a command: {cmd}")

    async def read_file(self, path: str) -> bytes:
        raise AssertionError(f"control tool read a file: {path}")

    async def write_file(self, path: str, data: bytes) -> None:
        raise AssertionError(f"control tool wrote a file: {path}")

    async def destroy(self) -> None:
        raise AssertionError("control tool destroyed the sandbox")


def test_finish_has_a_spec_the_model_can_call() -> None:
    spec = Finish().spec
    assert spec.name == FINISH == "finish"
    assert spec.input_schema["required"] == ["summary"]
    assert spec.input_schema["properties"]["summary"]["type"] == "string"
    assert spec.input_schema["additionalProperties"] is False


def test_finish_does_not_mutate_the_workspace() -> None:
    assert Finish().mutates is False


def test_the_loop_can_recognise_control_tools_by_name() -> None:
    assert set(CONTROL_TOOL_NAMES) == {"finish"}
    assert is_control_tool("finish")
    for name in ("run_tests", "run_command", "git_diff", "edit_file", ""):
        assert not is_control_tool(name)


async def test_finish_run_never_touches_the_sandbox() -> None:
    r = await Finish().run(UntouchableSandbox(), {"summary": "done"}, CTX)
    assert r.ok
    assert "agent loop" in r.output
    assert "done" not in r.output


async def test_finish_through_the_registry_is_safe_too() -> None:
    reg = default_registry()
    call = ToolCall(id="c1", name="finish", input={"summary": "Changed add() to return a + b."})
    r = await reg.run(call, UntouchableSandbox(), CTX)
    assert r.ok and r.call_id == "c1"


async def test_finish_still_needs_a_summary() -> None:
    reg = default_registry()
    for bad in ({}, {"summary": ""}, {"summary": 3}, {"summary": "ok", "extra": 1}):
        r = await reg.run(ToolCall(id="c", name="finish", input=bad), UntouchableSandbox(), CTX)
        assert not r.ok, bad


def test_finish_is_offered_to_the_model_by_default() -> None:
    names = [s.name for s in default_registry().specs()]
    assert FINISH in names
    assert all(is_control_tool(n) == (n == FINISH) for n in names)
