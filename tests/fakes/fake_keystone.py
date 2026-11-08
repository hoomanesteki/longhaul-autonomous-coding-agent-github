"""A stand-in for a Keystone gateway: an MCP endpoint that allows, denies or holds a call.

It speaks the same MCP over HTTP that Keystone does and answers the way Keystone's own tests
show it answering. A refusal is a tool result with `isError` set, a text of the form
`[keystone:<CODE>] call <id> <verb>: <message>`, and the facts again in `_meta` under the keys
`keystone/code` and `keystone/call_id` (Keystone ADR-0007). A call held for a person ends with
`KS_APPROVAL_PENDING` and `keystone/approval_id`, and `keystone__await_approval` settles it.

The fake decides by what the command says: a command with `curl` in it is denied, one with
`deploy` in it needs approval, and anything else is allowed and answered with an exit code line,
as the sandbox tool would. It records every call with the MCP session id and the bearer token
the request carried, which is what the tests of the session and the credentials read.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import mcp_types as types
import uvicorn
from mcp.server import Server, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.shared.exceptions import MCPError
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, MODERN_PROTOCOL_VERSIONS
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.types import ASGIApp

UPSTREAM = "longhaul_sandbox"
AWAIT_TOOL = "keystone__await_approval"
DENY_WORD = "curl"
HOLD_WORD = "deploy"
# A command with this word makes the worker's server fail, and one with that word makes it
# answer too late: Keystone reports them as KS_UPSTREAM_ERROR and KS_UPSTREAM_TIMEOUT.
FAULT_WORD = "explode"
SLOW_WORD = "stall"
SANDBOX_TOOLS = ("run_command", "read_file", "write_file")


@dataclass
class Seen:
    """One tools/call as the fake received it."""

    tool: str
    arguments: dict[str, Any]
    session: str | None
    authorization: str | None


@dataclass
class FakeKeystone:
    url: str = ""
    calls: list[Seen] = field(default_factory=list)
    # Approval ids by what a person decided: "approved", "rejected" or nothing yet.
    decisions: dict[str, str] = field(default_factory=dict)
    held: dict[str, Seen] = field(default_factory=dict)
    # Approval ids whose time ran out before anyone used them (Keystone's approvals.ttl_seconds).
    expired: set[str] = field(default_factory=set)
    # Set to make every call fail the way a gateway that is down does.
    broken: bool = False
    _issued: int = 0

    def decide(self, approval_id: str, verdict: str) -> None:
        self.decisions[approval_id] = verdict

    def expire(self, approval_id: str) -> None:
        self.expired.add(approval_id)

    def sessions(self) -> list[str | None]:
        return [seen.session for seen in self.calls]

    # ---------- the MCP handlers ----------
    async def list_tools(
        self, ctx: ServerRequestContext[Any, Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        schema = {"type": "object", "additionalProperties": True}
        names = [f"{UPSTREAM}__{name}" for name in SANDBOX_TOOLS]
        return types.ListToolsResult(
            tools=[types.Tool(name=n, input_schema=schema) for n in [*names, AWAIT_TOOL]]
        )

    async def call_tool(
        self, ctx: ServerRequestContext[Any, Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        headers = ctx.request.headers if ctx.request is not None else {}
        seen = Seen(
            tool=params.name,
            arguments=dict(params.arguments or {}),
            session=headers.get("mcp-session-id"),
            authorization=headers.get("authorization"),
        )
        self.calls.append(seen)
        if self.broken:
            raise RuntimeError("the gateway is down")
        if params.name == AWAIT_TOOL:
            return self._await(seen)
        return self._decide(seen)

    def _decide(self, seen: Seen) -> types.CallToolResult:
        command = str(seen.arguments.get("command", ""))
        call_id = f"call-{len(self.calls)}"
        if DENY_WORD in command:
            return _refusal(
                "KS_DENIED", call_id, "denied", "policy block-curl forbids this command", {}
            )
        if FAULT_WORD in command:
            return _refusal(
                "KS_UPSTREAM_ERROR", call_id, "failed", "the upstream server failed: boom", {}
            )
        if SLOW_WORD in command:
            return _refusal(
                "KS_UPSTREAM_TIMEOUT",
                call_id,
                "failed",
                "the upstream server did not answer within 30 s",
                {},
            )
        if HOLD_WORD in command:
            self._issued += 1
            approval_id = f"apr-{self._issued}"
            self.held[approval_id] = seen
            return _refusal(
                "KS_APPROVAL_PENDING",
                call_id,
                "waiting",
                "policy approve-deploy needs a person to approve this command",
                {"keystone/approval_id": approval_id},
            )
        return _allowed(f"exit code: 0\nran {command}" if command else "exit code: 0\nok")

    def _await(self, seen: Seen) -> types.CallToolResult:
        approval_id = str(seen.arguments.get("approval_id", ""))
        call_id = f"call-{len(self.calls)}"
        held = self.held.get(approval_id)
        verdict = self.decisions.get(approval_id)
        if held is None:
            return _refusal("KS_DENIED", call_id, "denied", "there is no such approval request", {})
        # Keystone lets only the session that asked use a request, and only within its time.
        if held.session != seen.session:
            return _refusal(
                "KS_DENIED",
                call_id,
                "denied",
                "there is no such approval request for this session",
                {},
            )
        if approval_id in self.expired:
            return _refusal(
                "KS_APPROVAL_EXPIRED",
                call_id,
                "expired",
                "the request expired before it was used",
                {},
            )
        if verdict == "approved":
            command = str(held.arguments.get("command", ""))
            return _allowed(f"exit code: 0\nran {command} (approved)")
        if verdict == "rejected":
            return _refusal(
                "KS_APPROVAL_REJECTED", call_id, "rejected", "a person rejected this call", {}
            )
        return _refusal(
            "KS_APPROVAL_PENDING",
            call_id,
            "waiting",
            "still waiting for a person",
            {"keystone/approval_id": approval_id},
        )


def _allowed(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(text=text)])


def _refusal(
    code: str, call_id: str, verb: str, message: str, extra: dict[str, Any]
) -> types.CallToolResult:
    meta = {**extra, "keystone/code": code, "keystone/call_id": call_id}
    text = f"[keystone:{code}] call {call_id} {verb}: {message}"
    return types.CallToolResult(
        content=[types.TextContent(text=text, _meta=meta)], is_error=True, _meta=meta
    )


@asynccontextmanager
async def serve_app(app: ASGIApp) -> AsyncIterator[str]:
    """Serve an ASGI app on a free port of this machine, in the running event loop, and yield the
    URL of its /mcp route."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", timeout_graceful_shutdown=2
    )
    http = uvicorn.Server(config)
    task = asyncio.create_task(http.serve())
    try:
        while not http.started:
            if task.done():
                task.result()
                raise RuntimeError("the test server exited before it started")
            await asyncio.sleep(0.01)
        port = http.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        http.should_exit = True
        await task


@asynccontextmanager
async def serve_mcp(server: Server[Any]) -> AsyncIterator[str]:
    """Serve a low-level MCP server over streamable HTTP and yield its /mcp URL."""
    manager = StreamableHTTPSessionManager(app=server)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = Starlette(
        routes=[Route("/mcp", endpoint=StreamableHTTPASGIApp(manager))], lifespan=lifespan
    )
    async with serve_app(app) as url:
        yield url


async def handshake_era_only(
    ctx: ServerRequestContext[Any, Any], call_next: CallNext
) -> HandlerResult:
    """Keystone speaks the era with sessions only, so its state can follow a session id. A client
    that tried the newer era is told what is supported and falls back to `initialize`."""
    if ctx.protocol_version in MODERN_PROTOCOL_VERSIONS:
        raise MCPError(
            code=types.UNSUPPORTED_PROTOCOL_VERSION,
            message="Unsupported protocol version",
            data={
                "supported": list(HANDSHAKE_PROTOCOL_VERSIONS),
                "requested": ctx.protocol_version,
            },
        )
    return await call_next(ctx)


@asynccontextmanager
async def running_keystone() -> AsyncIterator[FakeKeystone]:
    fake = FakeKeystone()
    server: Server[Any] = Server(
        "keystone", on_list_tools=fake.list_tools, on_call_tool=fake.call_tool
    )
    server.middleware.insert(0, handshake_era_only)
    async with serve_mcp(server) as url:
        fake.url = url
        yield fake
