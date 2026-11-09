"""The longhaul-sandbox MCP server: the sandbox tools, served so that Keystone can call them.

With `tools.backend: keystone` the worker serves this app, and Keystone registers it as an
upstream. An agent call then goes worker -> Keystone -> this server -> the sandbox, and Keystone
decides it, audits it and may hold it for a person on the way. See docs/runbook.md for how to
register the upstream.

What the server exposes. Every tool of the registry that touches the sandbox: `finish` is
handled by the loop and `read_output` reads a stored blob, so neither runs here. Each tool keeps
its own input schema and gets one more property, `_longhaul`, which the worker fills in. It names
the task and the sandbox the call is for, because Keystone connects as one client for all tasks
and the server cannot tell them apart by its connection. The property is required, so Keystone
checks it with the rest of the arguments, and it is taken off before the tool sees the input.

A tool runs through the registry, so the checks and the messages are the same as in direct mode:
input the model got wrong comes back as an error result, with the registry's words.

Who may call it. The tools run commands, so a request without the shared key is refused with 401
before any tool is looked up. Keystone sends the key as a credential header (`X-Api-Key`), and
the worker will not start the server without one (LONGHAUL_SANDBOX_MCP_KEY). The server listens
on the loopback address unless the config says otherwise. It keeps no state between requests, so
a restart of the worker costs Keystone nothing but the calls that were running.
"""

from __future__ import annotations

import asyncio
import copy
import hmac
import logging
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Protocol

import mcp_types as types
import uvicorn
from mcp.server import Server, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from longhaul.contracts import Sandbox, ToolCall, ToolContext, ToolResult
from longhaul.tools.control import CONTROL_TOOL_NAMES
from longhaul.tools.registry import ToolRegistry

log = logging.getLogger(__name__)

SERVER_NAME = "longhaul-sandbox"
# The argument that carries the task and the sandbox of a call. It starts with an underscore so
# that it cannot be one of a tool's own arguments.
CONTEXT_KEY = "_longhaul"
KEY_HEADER = "x-api-key"
EXIT_CODE_KEY = "exit_code"
# Tools that run in the worker and not in the sandbox.
LOCAL_TOOLS = CONTROL_TOOL_NAMES | {"read_output"}
MIN_KEY_CHARS = 16


class SandboxLookup(Protocol):
    """Find a running sandbox by its id: the part of a SandboxManager that the server uses."""

    async def get(self, sandbox_id: str) -> Sandbox | None: ...


class CallContext(BaseModel):
    """What the worker sends with a call, in the `_longhaul` argument."""

    task_id: str
    sandbox_id: str
    turn_index: int
    output_max_tokens: int = 4000


def exposed_names(registry: ToolRegistry) -> frozenset[str]:
    """The names of the tools that this server serves."""
    return frozenset(spec.name for spec in registry.specs() if spec.name not in LOCAL_TOOLS)


def with_context(schema: Mapping[str, Any]) -> dict[str, Any]:
    """A tool's input schema with the required `_longhaul` property added."""
    found = copy.deepcopy(dict(schema))
    properties = found.setdefault("properties", {})
    properties[CONTEXT_KEY] = {
        "type": "object",
        "description": "Set by the Longhaul worker: the task and the sandbox this call is for.",
        "properties": {
            "task_id": {"type": "string"},
            "sandbox_id": {"type": "string"},
            "turn_index": {"type": "integer"},
            "output_max_tokens": {"type": "integer"},
        },
        "required": ["task_id", "sandbox_id", "turn_index"],
    }
    found["required"] = [*found.get("required", []), CONTEXT_KEY]
    return found


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=message)], is_error=True)


class SandboxTools:
    """The two handlers of the server, with the registry and the sandboxes they use."""

    def __init__(self, registry: ToolRegistry, sandboxes: SandboxLookup) -> None:
        self._registry = registry
        self._sandboxes = sandboxes
        self._names = exposed_names(registry)

    async def list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        tools = [
            types.Tool(
                name=spec.name,
                description=spec.description,
                input_schema=with_context(spec.input_schema),
            )
            for spec in self._registry.specs()
            if spec.name in self._names
        ]
        return types.ListToolsResult(tools=tools)

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name not in self._names:
            return _error(f"unknown tool: {params.name}")
        arguments = dict(params.arguments or {})
        try:
            context = CallContext.model_validate(arguments.pop(CONTEXT_KEY, None))
        except ValidationError:
            return _error(f"the call has no valid {CONTEXT_KEY} argument")
        sandbox = await self._sandboxes.get(context.sandbox_id)
        if sandbox is None:
            return _error(f"sandbox {context.sandbox_id} is gone")
        tool_ctx = ToolContext(
            task_id=context.task_id,
            turn_index=context.turn_index,
            blob_prefix=f"tasks/{context.task_id}",
            output_max_tokens=context.output_max_tokens,
        )
        # The id is the worker's to set: it knows which tool_use block the call answers.
        call = ToolCall(id="", name=params.name, input=arguments)
        return to_mcp(await self._registry.run(call, sandbox, tool_ctx))


def to_mcp(result: ToolResult) -> types.CallToolResult:
    """The tool's text as a text block, and a failed tool as an error result.

    The exit code goes along as structured content: the text of a command that timed out does not
    start with it, and the stuck detector reads the code.
    """
    structured = None if result.exit_code is None else {EXIT_CODE_KEY: result.exit_code}
    return types.CallToolResult(
        content=[types.TextContent(text=result.output)],
        structured_content=structured,
        is_error=not result.ok,
    )


def build_server(registry: ToolRegistry, sandboxes: SandboxLookup) -> Server[Any]:
    tools = SandboxTools(registry, sandboxes)
    return Server(SERVER_NAME, on_list_tools=tools.list_tools, on_call_tool=tools.call_tool)


class _KeyMiddleware:
    """Refuse a request that does not carry the shared key. Compared in constant time."""

    def __init__(self, app: ASGIApp, key: str) -> None:
        self.app = app
        self._key = key.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            given = dict(scope["headers"]).get(KEY_HEADER.encode(), b"")
            if not hmac.compare_digest(given, self._key):
                response = JSONResponse({"error": "invalid or missing key"}, status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app(registry: ToolRegistry, sandboxes: SandboxLookup, *, key: str) -> Starlette:
    """The ASGI app that serves the tools at /mcp, behind the key."""
    if len(key) < MIN_KEY_CHARS:
        raise ValueError(
            f"the sandbox MCP server needs a key of at least {MIN_KEY_CHARS} characters "
            "(set LONGHAUL_SANDBOX_MCP_KEY)"
        )
    # Stateless: Keystone opens its own session to each upstream and may reconnect at any time,
    # and nothing here depends on a session. A call carries everything it needs.
    manager = StreamableHTTPSessionManager(app=build_server(registry, sandboxes), stateless=True)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(
        routes=[Route("/mcp", endpoint=StreamableHTTPASGIApp(manager))], lifespan=lifespan
    )
    app.add_middleware(_KeyMiddleware, key=key)
    return app


class _WorkerServer(uvicorn.Server):
    """A uvicorn server that leaves the signals alone.

    uvicorn takes SIGINT and SIGTERM for itself while it serves. The worker stops on those two
    (durable/worker.py), and it has to keep stopping on them.
    """

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@asynccontextmanager
async def serving(app: ASGIApp, host: str, port: int) -> AsyncIterator[int]:
    """Serve `app` with uvicorn in the running event loop. Yields the port it listens on."""
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=2
    )
    server = _WorkerServer(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            if task.done():
                task.result()
                raise RuntimeError(f"the sandbox MCP server could not listen on {host}:{port}")
            await asyncio.sleep(0.01)
        bound: int = server.servers[0].sockets[0].getsockname()[1]
        log.info("sandbox MCP server on http://%s:%d/mcp", host, bound)
        yield bound
    finally:
        server.should_exit = True
        await task
