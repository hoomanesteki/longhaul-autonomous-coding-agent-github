"""The longhaul-sandbox MCP server (LH-909).

In keystone mode the worker serves its sandbox tools over MCP, and Keystone calls them as an
upstream. The server runs each tool in the sandbox that the call names, with the registry's own
checks, and answers with the tool's text. Only Keystone may call it: a request without the shared
key is refused before any tool is found, because the tools run commands.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import mcp_types as types
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from tests.fakes.fake_keystone import serve_app

from longhaul.contracts import Sandbox
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.mcp_server import (
    CONTEXT_KEY,
    KEY_HEADER,
    SERVER_NAME,
    build_app,
    build_server,
    exposed_names,
)
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

KEY = "shared-key-for-the-test"


class Boxes:
    def __init__(self, *boxes: FakeSandbox) -> None:
        self.boxes = {box.id: box for box in boxes}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)


@pytest.fixture
async def sandbox() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox("sbx-1")
    await box.write_file("notes.txt", b"one\ntwo\n")
    yield box
    await box.destroy()


def context(sandbox_id: str = "sbx-1") -> dict[str, Any]:
    return {
        "task_id": "lh-task-1",
        "sandbox_id": sandbox_id,
        "turn_index": 2,
        "output_max_tokens": 4000,
    }


def text_of(result: types.CallToolResult) -> str:
    return "".join(b.text for b in result.content if isinstance(b, types.TextContent))


@asynccontextmanager
async def connected(sandbox: FakeSandbox) -> AsyncIterator[Client]:
    """A client of the server, in memory. It is opened inside the test and not in a fixture,
    because the SDK needs to enter and leave a client in the same task."""
    server = build_server(default_registry(), Boxes(sandbox))
    async with Client(server) as found:
        yield found


# ---------- the tool list ----------
def test_the_sandbox_tools_are_exposed_and_the_local_ones_are_not() -> None:
    names = exposed_names(default_registry())

    assert {"run_command", "read_file", "edit_file", "write_file", "search", "git_diff"} <= names
    assert "run_tests" in names and "list_dir" in names
    assert "finish" not in names
    assert "read_output" not in names


async def test_the_server_is_called_longhaul_sandbox(sandbox: FakeSandbox) -> None:
    async with connected(sandbox) as client:
        assert client.server_info is not None
        name = client.server_info.name

    assert SERVER_NAME == "longhaul-sandbox"
    assert name == SERVER_NAME


async def test_every_tool_requires_the_context_the_worker_sends(sandbox: FakeSandbox) -> None:
    async with connected(sandbox) as client:
        listed = await client.list_tools()

    names = {tool.name for tool in listed.tools}
    assert names == exposed_names(default_registry())
    for tool in listed.tools:
        assert CONTEXT_KEY in tool.input_schema["properties"]
        assert CONTEXT_KEY in tool.input_schema["required"]
    # The tool's own arguments are as the registry has them.
    run_command = next(t for t in listed.tools if t.name == "run_command")
    assert "command" in run_command.input_schema["properties"]
    assert "command" in run_command.input_schema["required"]


# ---------- calls ----------
async def test_a_call_runs_in_the_sandbox_the_context_names(sandbox: FakeSandbox) -> None:
    async with connected(sandbox) as client:
        result = await client.call_tool(
            "run_command", {"command": "cat notes.txt", CONTEXT_KEY: context()}
        )

    assert not result.is_error
    assert text_of(result).startswith("exit code: 0")
    assert "one" in text_of(result)


async def test_a_command_that_exits_nonzero_keeps_its_exit_code(sandbox: FakeSandbox) -> None:
    # As in direct mode, a command that exits with an error ran: it is a result, not a failure of
    # the tool. Only a timeout is one.
    async with connected(sandbox) as client:
        result = await client.call_tool(
            "run_command", {"command": "exit 3", CONTEXT_KEY: context()}
        )

    assert not result.is_error
    assert text_of(result).startswith("exit code: 3")
    assert result.structured_content == {"exit_code": 3}


async def test_a_command_that_times_out_is_an_error_result_with_its_exit_code(
    sandbox: FakeSandbox,
) -> None:
    async with connected(sandbox) as client:
        result = await client.call_tool(
            "run_command", {"command": "sleep 5", "timeout_s": 1, CONTEXT_KEY: context()}
        )

    assert result.is_error
    assert text_of(result).startswith("timed out after 1 s")
    assert isinstance(result.structured_content, dict)
    assert "exit_code" in result.structured_content


async def test_input_the_model_got_wrong_is_an_error_result_with_the_registrys_words(
    sandbox: FakeSandbox,
) -> None:
    async with connected(sandbox) as client:
        result = await client.call_tool(
            "read_file", {"path": "missing.txt", CONTEXT_KEY: context()}
        )

    assert result.is_error
    assert "missing.txt" in text_of(result)


async def test_a_call_without_the_context_is_refused(sandbox: FakeSandbox) -> None:
    async with connected(sandbox) as client:
        result = await client.call_tool("run_command", {"command": "echo hi"})

    assert result.is_error
    assert CONTEXT_KEY in text_of(result)


async def test_a_call_for_a_sandbox_that_is_gone_is_refused(sandbox: FakeSandbox) -> None:
    async with connected(sandbox) as client:
        result = await client.call_tool(
            "run_command", {"command": "echo hi", CONTEXT_KEY: context("sbx-9")}
        )

    assert result.is_error
    assert "sbx-9" in text_of(result)


async def test_a_tool_the_server_does_not_expose_is_refused(sandbox: FakeSandbox) -> None:
    async with connected(sandbox) as client:
        result = await client.call_tool("finish", {"summary": "done", CONTEXT_KEY: context()})

    assert result.is_error
    assert "unknown tool" in text_of(result)


async def test_the_context_is_not_passed_on_to_the_tool(sandbox: FakeSandbox) -> None:
    # read_file's schema forbids extra properties, so passing the context on would fail it.
    async with connected(sandbox) as client:
        result = await client.call_tool("read_file", {"path": "notes.txt", CONTEXT_KEY: context()})

    assert not result.is_error
    assert "one" in text_of(result)


async def test_run_tests_runs_the_test_command_the_context_names(sandbox: FakeSandbox) -> None:
    # The worker registry has no test command here, as a repo without one. The task's own
    # command arrives with the call and is what runs, as it does in direct mode.
    async with connected(sandbox) as client:
        result = await client.call_tool(
            "run_tests", {CONTEXT_KEY: {**context(), "test_command": "echo ran-the-task-command"}}
        )

    assert not result.is_error
    assert "ran-the-task-command" in text_of(result)


async def test_a_test_command_in_the_context_changes_only_run_tests(sandbox: FakeSandbox) -> None:
    async with connected(sandbox) as client:
        result = await client.call_tool(
            "run_command",
            {"command": "echo plain", CONTEXT_KEY: {**context(), "test_command": "false"}},
        )

    assert text_of(result).startswith("exit code: 0")
    assert "plain" in text_of(result)


async def test_run_tests_without_a_command_in_the_context_uses_the_worker_default(
    sandbox: FakeSandbox,
) -> None:
    async with connected(sandbox) as client:
        result = await client.call_tool("run_tests", {CONTEXT_KEY: context()})

    assert result.is_error
    assert "no test command" in text_of(result)


# ---------- the key ----------
async def test_a_request_without_the_key_is_refused(sandbox: FakeSandbox) -> None:
    app = build_app(default_registry(), Boxes(sandbox), key=KEY)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://worker") as http:
        missing = await http.post("/mcp", json={})
        wrong = await http.post("/mcp", json={}, headers={KEY_HEADER: "not-the-key"})

    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_a_server_without_a_key_cannot_be_built(sandbox: FakeSandbox) -> None:
    with pytest.raises(ValueError, match="key"):
        build_app(default_registry(), Boxes(sandbox), key="")


async def test_a_client_with_the_key_runs_a_tool_over_http(sandbox: FakeSandbox) -> None:
    app = build_app(default_registry(), Boxes(sandbox), key=KEY)

    async with serve_app(app) as url:
        async with (
            httpx2.AsyncClient(headers={KEY_HEADER: KEY}) as http,
            Client(streamable_http_client(url, http_client=http)) as found,
        ):
            result = await found.call_tool(
                "read_file", {"path": "notes.txt", CONTEXT_KEY: context()}
            )
        async with httpx2.AsyncClient() as http:
            refused = await http.post(url, json={})

    assert not result.is_error
    assert "two" in text_of(result)
    assert refused.status_code == 401
