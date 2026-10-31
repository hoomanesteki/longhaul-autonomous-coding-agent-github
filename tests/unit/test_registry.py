from collections.abc import AsyncIterator
from typing import Any

import docker.errors
import pytest
import requests
from jsonschema.exceptions import SchemaError

from longhaul.contracts import Sandbox, ToolCall, ToolContext, ToolResult, ToolSpec
from longhaul.providers.openai_compat import _parse_arguments
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools import ToolError
from longhaul.tools.paths import PathError
from longhaul.tools.registry import ToolRegistry, default_registry

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


class RaisingTool(EchoTool):
    """A tool that raises whatever it was built with."""

    spec = ToolSpec(name="raiser", description="Raises.", input_schema={"type": "object"})

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raise self.exc


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


# ---------- which errors become results ----------

NOT_UTF8 = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

# (error, what the model is told). A ToolError carries its own message for the model.
# The built-in errors name their type, because their text alone, such as a bare path,
# says nothing.
BAD_INPUT = [
    (ToolError("end_line is before start_line"), "end_line is before start_line"),
    (PathError("path is outside /workspace: /etc"), "path is outside /workspace: /etc"),
    (FileNotFoundError("a.py"), "FileNotFoundError: a.py"),
    (IsADirectoryError("/workspace/src"), "IsADirectoryError: /workspace/src"),
    (NotADirectoryError("/workspace/a.py/b"), "NotADirectoryError: /workspace/a.py/b"),
    (FileExistsError("/workspace/x"), "FileExistsError: /workspace/x"),
    (PermissionError("denied"), "PermissionError: denied"),
    (NOT_UTF8, f"UnicodeDecodeError: {NOT_UTF8}"),
]


@pytest.mark.parametrize(("exc", "told"), BAD_INPUT, ids=[type(e).__name__ for e, _ in BAD_INPUT])
async def test_bad_input_errors_are_results(exc: Exception, told: str, sbx: FakeSandbox) -> None:
    reg = ToolRegistry()
    reg.register(RaisingTool(exc))
    r = await reg.run(ToolCall(id="c", name="raiser", input={}), sbx, CTX)
    assert not r.ok and r.call_id == "c"
    assert r.output == told


async def test_a_write_below_a_file_is_a_result_and_leaves_the_file_alone(
    sbx: FakeSandbox,
) -> None:
    # The real tool and a real type conflict. DockerSandbox raises NotADirectoryError here,
    # and FakeSandbox raises one of the two errors that a mkdir below a file can give.
    await sbx.write_file("a.py", b"x\n")
    call = ToolCall(id="c", name="write_file", input={"path": "a.py/b.txt", "content": "y"})
    r = await default_registry().run(call, sbx, CTX)
    assert not r.ok and r.call_id == "c"
    assert r.output.split(":")[0] in {"NotADirectoryError", "FileExistsError"}, r.output
    assert await sbx.read_file("a.py") == b"x\n"


# What goes wrong around the sandbox is not the model's doing. It has to reach the caller,
# so a lost container can be recreated and a dropped connection retried.
INFRASTRUCTURE = [
    docker.errors.NotFound("No such container: 8b8659cf"),
    docker.errors.APIError("500 Server Error: daemon unavailable"),
    docker.errors.DockerException("Error while fetching server API version"),
    requests.exceptions.ConnectionError("Connection aborted"),
    ConnectionResetError("reset by peer"),
    TimeoutError("timed out"),
    OSError("input/output error"),
    RuntimeError("kaput"),
]


@pytest.mark.parametrize("exc", INFRASTRUCTURE, ids=[type(e).__name__ for e in INFRASTRUCTURE])
async def test_other_errors_propagate(exc: Exception, sbx: FakeSandbox) -> None:
    reg = ToolRegistry()
    reg.register(RaisingTool(exc))
    with pytest.raises(type(exc)) as seen:
        await reg.run(ToolCall(id="c", name="raiser", input={}), sbx, CTX)
    assert seen.value is exc


async def test_an_unexpected_exception_in_a_tool_is_not_hidden(
    registry: ToolRegistry, sbx: FakeSandbox
) -> None:
    with pytest.raises(RuntimeError, match="kaput"):
        await registry.run(ToolCall(id="c5", name="boom", input={}), sbx, CTX)


# ---------- unparseable arguments ----------

# The OpenAI-compatible provider keeps text it could not parse as {"_raw": text}. This is
# a reply cut off at max_tokens, which is how it happens with a long write_file.
CUT_OFF = '{"path": "src/big.py", "content": "def f():\\n    return 1\\n'


@pytest.mark.parametrize("name", [s.name for s in default_registry().specs()])
async def test_unparseable_arguments_say_so(name: str, sbx: FakeSandbox) -> None:
    r = await default_registry().run(ToolCall(id="c", name=name, input={"_raw": CUT_OFF}), sbx, CTX)
    assert not r.ok and r.call_id == "c"
    assert f"arguments for {name} were not a complete JSON object" in r.output
    assert "max_tokens" in r.output
    assert "required property" not in r.output and "Additional properties" not in r.output


async def test_arguments_that_parse_to_something_else_are_unparseable_too(
    sbx: FakeSandbox,
) -> None:
    # The provider also uses {"_raw": text} when the JSON is a list or a number.
    r = await default_registry().run(
        ToolCall(id="c", name="read_file", input={"_raw": "[1, 2]"}), sbx, CTX
    )
    assert not r.ok and "not a complete JSON object" in r.output


@pytest.mark.parametrize("raw", ["", "  ", "\n"])
async def test_blank_arguments_mean_no_arguments(raw: str, sbx: FakeSandbox) -> None:
    # Some servers send an empty string for a call that has no parameters.
    reg = default_registry()
    listing = await reg.run(ToolCall(id="c", name="list_dir", input={"_raw": raw}), sbx, CTX)
    assert listing.ok, listing.output
    missing = await reg.run(ToolCall(id="c", name="read_file", input={"_raw": raw}), sbx, CTX)
    assert not missing.ok and "'path' is a required property" in missing.output


async def test_the_registry_reads_the_arguments_the_openai_provider_makes(
    sbx: FakeSandbox,
) -> None:
    # The input comes from the provider's own parser, so the two sides cannot drift apart
    # on the key or on what counts as unparseable.
    reg = default_registry()
    for text in (CUT_OFF, "[1, 2]", '"just text"', "{"):
        call = ToolCall(id="c", name="write_file", input=_parse_arguments(text))
        r = await reg.run(call, sbx, CTX)
        assert not r.ok and "not a complete JSON object" in r.output, text
    whole = _parse_arguments('{"path": "a.txt", "content": "hi"}')
    made = await reg.run(ToolCall(id="c", name="write_file", input=whole), sbx, CTX)
    assert made.ok, made.output
    # A server that sends "" for a call without parameters.
    listing = await reg.run(ToolCall(id="c", name="list_dir", input=_parse_arguments("")), sbx, CTX)
    assert listing.ok, listing.output


async def test_raw_next_to_other_keys_is_ordinary_unknown_input(sbx: FakeSandbox) -> None:
    r = await default_registry().run(
        ToolCall(id="c", name="list_dir", input={"_raw": "x", "depth": 1}), sbx, CTX
    )
    assert not r.ok and "Additional properties" in r.output


# ---------- the real tool specs ----------

TOOL_NAMES = [s.name for s in default_registry().specs()]

# Written out here and not read from the specs: a spec that lost its "required" list
# must make a test fail, not make the test skip its own checks.
REQUIRED = {
    "list_dir": [],
    "read_file": ["path"],
    "edit_file": ["path", "old_str", "new_str"],
    "write_file": ["path", "content"],
    "search": ["pattern"],
    "run_command": ["command"],
    "run_tests": [],
    "git_diff": [],
    "finish": ["summary"],
}


def test_the_required_table_covers_every_registered_tool() -> None:
    assert list(REQUIRED) == TOOL_NAMES


@pytest.mark.parametrize(("name", "required"), REQUIRED.items(), ids=list(REQUIRED))
async def test_every_required_input_is_checked_on_the_real_specs(
    name: str, required: list[str], sbx: FakeSandbox
) -> None:
    reg = default_registry(test_command="pytest -q")
    for key in required:
        others = {k: "x" for k in required if k != key}
        r = await reg.run(ToolCall(id="c", name=name, input=others), sbx, CTX)
        assert not r.ok, (name, key)
        assert f"'{key}' is a required property" in r.output, (name, key, r.output)


@pytest.mark.parametrize(
    ("name", "args", "fragment"),
    [
        ("read_file", {"path": 5}, "at path: 5 is not of type 'string'"),
        ("read_file", {"path": "a", "start_line": 0}, "at start_line: 0 is less than the minimum"),
        ("read_file", {"path": "a", "end_line": "9"}, "at end_line: '9' is not of type"),
        ("list_dir", {"depth": 9}, "at depth: 9 is greater than the maximum of 6"),
        ("list_dir", {"depth": 0}, "at depth: 0 is less than the minimum of 1"),
        ("list_dir", {"where": "."}, "Additional properties are not allowed"),
        ("search", {"pattern": "x", "max_results": 0}, "at max_results"),
        ("search", {"pattern": "x", "max_results": 5000}, "at max_results"),
        ("search", {"pattern": ""}, "at pattern"),
        ("edit_file", {"path": "a", "old_str": "", "new_str": "b"}, "at old_str"),
        ("write_file", {"path": "a", "content": 3}, "at content: 3 is not of type 'string'"),
        ("run_command", {"command": "ls", "timeout_s": 0}, "at timeout_s"),
        ("git_diff", {"staged": "yes"}, "at staged: 'yes' is not of type 'boolean'"),
        ("finish", {"summary": ""}, "at summary"),
    ],
)
async def test_wrong_input_to_a_real_tool_is_a_result(
    name: str, args: dict[str, Any], fragment: str, sbx: FakeSandbox
) -> None:
    r = await default_registry().run(ToolCall(id="c", name=name, input=args), sbx, CTX)
    assert not r.ok
    assert r.output.startswith(f"invalid input for {name}")
    assert fragment in r.output, r.output


def test_a_malformed_schema_is_refused_when_the_tool_is_registered() -> None:
    class TypoTool(EchoTool):
        spec = ToolSpec(
            name="typo",
            description="Has a schema nobody can validate against.",
            input_schema={"type": "object", "properties": {"n": {"type": "integr"}}},
        )

    reg = ToolRegistry()
    with pytest.raises(SchemaError):
        reg.register(TypoTool())
    assert reg.get("typo") is None
    # The name is still free, so a corrected tool can be registered.
    reg.register(EchoTool())


# ---------- what the checkpoint logic reads ----------

# LH-206 commits a checkpoint after a tool call only when the tool says it mutates.
# The exec tools say False on purpose: that story looks for a dirty tree after them.
EXPECTED_MUTATES = {
    "list_dir": False,
    "read_file": False,
    "edit_file": True,
    "write_file": True,
    "search": False,
    "run_command": False,
    "run_tests": False,
    "git_diff": False,
    "finish": False,
}


def test_mutates_flags_of_every_registered_tool() -> None:
    reg = default_registry()
    flags: dict[str, bool] = {}
    for spec in reg.specs():
        tool = reg.get(spec.name)
        assert tool is not None
        flags[spec.name] = tool.mutates
    assert flags == EXPECTED_MUTATES
