"""The Keystone backend: every sandbox tool call goes through a Keystone gateway first (LH-909).

Keystone is a separate project, a gateway that sits in front of tool servers. It checks each
call against policy, may hold it for a person, and writes it to an audit log. With
`tools.backend: keystone` the worker serves its sandbox tools as an MCP server (mcp_server.py),
which Keystone knows as an upstream, and `tool_exec` calls the tool through Keystone's MCP
endpoint, as an MCP client. Keystone then calls the worker's server, and that runs the tool.

The decision comes back as a tool result (Keystone ADR-0007): a refusal has `isError` set, a
text of the form `[keystone:<CODE>] call <id> <verb>: <reason>`, and the code again in `_meta`
under `keystone/code`. This module maps it:

- allowed: the tool's own result, as it would have come from the sandbox;
- denied, or refused for any other reason (rate limit, budget, a rejected approval): a failed
  result, `ok=False`, with Keystone's text. The model reads the reason and tries something else;
- held for approval (`KS_APPROVAL_PENDING`): a failed result that says the call did not run,
  and an approval question for the workflow, which pauses the task through the pause for a hint
  (LH-506) with the reason as the question. When the person has decided in Keystone and answers
  the pause, the model calls the same tool again, and the backend settles the held request with
  Keystone's `keystone__await_approval` tool instead of asking a second time.

A request that Keystone holds can be used once, by the session that made it, within its
`approvals.ttl_seconds`. The backend keeps the request id in memory and drops it only once the
request is settled. When Keystone refuses the settling because the session is gone or the time ran
out, the model is told that the approval could not be used, and its next call asks anew.

Anything else that goes wrong is raised and not returned: a gateway that cannot be reached, and
Keystone's own faults (`KS_UPSTREAM_ERROR`, `KS_UPSTREAM_TIMEOUT`), which say that the worker's
tool server failed or did not answer. They are not the model's doing, and a result would hide
them and count them toward the stuck rule. Temporal runs the activity again, from the checkpoint,
so a command that had run is undone before it runs a second time. A call that Keystone could not
decide never runs. There is no fallback to the
direct backend, because the point of the mode is that nothing runs unchecked.

The session. Keystone keeps what a session has read (its flags and taint) per MCP session, and
the rules about untrusted text depend on it. So all the calls of a task share one session, and
a task never shares one with another. The backend keeps one client per task id. It also sends
the task id inside every call (the `_longhaul` argument), so that Keystone's audit log shows
which task made a call. The client is entered and left by one background task, because the SDK
ties its streams to the task that opened them and an activity runs in a task of its own.

The credential. Keystone signs callers in with a bearer token that expires, so the token is read
again for every request and not once at the start.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from longhaul.contracts import Sandbox, ToolCall, ToolContext, ToolResult
from longhaul.settings import EnvSettings, Tools
from longhaul.tools.backend import DirectBackend, Ran, ToolBackend
from longhaul.tools.exec import RunTests
from longhaul.tools.mcp_server import CONTEXT_KEY, EXIT_CODE_KEY, exposed_names
from longhaul.tools.registry import ToolRegistry

DEFAULT_UPSTREAM = "longhaul_sandbox"
SEPARATOR = "__"  # Keystone names a tool <upstream>__<tool>
AWAIT_TOOL = "keystone__await_approval"
CODE_KEY = "keystone/code"
APPROVAL_KEY = "keystone/approval_id"
PENDING = "KS_APPROVAL_PENDING"
REJECTED = "KS_APPROVAL_REJECTED"
UPSTREAM_ERROR = "KS_UPSTREAM_ERROR"
UPSTREAM_TIMEOUT = "KS_UPSTREAM_TIMEOUT"
# `[keystone:KS_DENIED] call 01J... denied: <reason>`
_REFUSAL = re.compile(r"^\[keystone:(?P<code>[A-Z_]+)\] call \S+ [^:]*: (?P<reason>.*)$", re.DOTALL)
_EXIT_LINE = re.compile(r"^exit code: (?P<code>-?\d+)")
_COMMAND_TOOLS = frozenset({"run_command", "run_tests"})

CONNECT_TIMEOUT_S = 10.0
# A command can run as long as the sandbox allows (600 s by default) and Keystone may hold the
# call for a person on top of that, so the wait for an answer is long.
CALL_TIMEOUT_S = 1800.0
# Tasks that keep a session open at once. The oldest session is closed past this, and its task
# starts a new one on its next call.
MAX_SESSIONS = 256
# Held requests remembered for a retry. A model that never calls again leaves its entry here.
MAX_HELD = 1024

TokenSource = Callable[[], str | None]


class KeystoneFault(RuntimeError):
    """Keystone could not get the call run, through no fault of the model.

    It is an ordinary exception, so Temporal retries the activity under its retry policy.
    """


class _Connection:
    """One MCP client session, held open by a task of its own until it is told to stop."""

    def __init__(self, url: str, sign: Callable[[httpx2.Request], Any]) -> None:
        self._url = url
        self._sign = sign
        self._client: Client | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._error: BaseException | None = None
        self._task = asyncio.create_task(self._hold())

    @property
    def alive(self) -> bool:
        return not self._task.done()

    async def _hold(self) -> None:
        try:
            http = httpx2.AsyncClient(
                timeout=httpx2.Timeout(CONNECT_TIMEOUT_S, read=CALL_TIMEOUT_S),
                follow_redirects=False,
                event_hooks={"request": [self._sign]},
            )
            async with http, Client(streamable_http_client(self._url, http_client=http)) as client:
                self._client = client
                self._ready.set()
                await self._stop.wait()
        except BaseException as exc:
            self._error = exc
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._ready.set()

    async def client(self) -> Client:
        """The client, once the session is open. Raises if it could not be opened."""
        try:
            await asyncio.wait_for(self._ready.wait(), CONNECT_TIMEOUT_S * 2)
        except TimeoutError:
            self.stop()
            raise
        if self._client is None or not self.alive:
            raise ConnectionError("could not open a session with Keystone") from self._error
        return self._client

    def stop(self) -> None:
        self._stop.set()

    async def close(self) -> None:
        self.stop()
        await asyncio.gather(self._task, return_exceptions=True)


class KeystoneBackend:
    """Calls each sandbox tool through Keystone, one MCP session per task."""

    remote = True

    def __init__(
        self,
        url: str,
        *,
        upstream: str = DEFAULT_UPSTREAM,
        token: TokenSource | None = None,
    ) -> None:
        self._url = url
        self._prefix = f"{upstream}{SEPARATOR}"
        self._token = token or (lambda: None)
        self._connections: OrderedDict[str, _Connection] = OrderedDict()
        self._opening = asyncio.Lock()
        # Approval requests that Keystone holds, by task and call, for the retry after the
        # person decided.
        self._held: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    def current_token(self) -> str | None:
        return self._token()

    async def _sign(self, request: httpx2.Request) -> None:
        token = self.current_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"

    # ---------- the call ----------
    async def run(
        self,
        registry: ToolRegistry,
        sandbox: Sandbox,
        call: ToolCall,
        ctx: ToolContext,
        *,
        sandbox_id: str,
    ) -> Ran:
        if call.name not in exposed_names(registry):
            # `finish`, `read_output` and a name nobody registered have no sandbox to guard.
            return await DirectBackend().run(registry, sandbox, call, ctx, sandbox_id=sandbox_id)
        began = time.monotonic()
        key = (ctx.task_id, call.name, _digest(call.input))
        # The entry stays until the request is settled, so that a retry of this activity after a
        # failed send settles the same request and does not hold the call a second time.
        approval_id = self._held.get(key)
        if approval_id is not None:
            sent = await self._send(ctx.task_id, AWAIT_TOOL, {"approval_id": approval_id})
        else:
            arguments = {**call.input, CONTEXT_KEY: _context(ctx, sandbox_id, registry, call)}
            sent = await self._send(ctx.task_id, self._prefix + call.name, arguments)
        elapsed = int((time.monotonic() - began) * 1000)
        reply = interpret(sent, call, elapsed)
        if reply.code in (UPSTREAM_ERROR, UPSTREAM_TIMEOUT):
            raise _fault(reply, call)
        result = reply.result
        if approval_id is not None and reply.code != PENDING:
            del self._held[key]  # used, rejected, expired or lost: the request is spent
            if not result.ok and reply.code not in (None, REJECTED):
                result = _unusable(result, call)
        if reply.approval_id is not None:
            self._held[key] = reply.approval_id
            while len(self._held) > MAX_HELD:
                self._held.popitem(last=False)
        return Ran(result, reply.question)

    async def _send(
        self, task_id: str, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        connection = await self._connection(task_id)
        try:
            client = await connection.client()
            async with asyncio.timeout(CALL_TIMEOUT_S):
                return await client.call_tool(name, arguments)
        except BaseException:
            # The session may be half dead, and a call may be in flight on it. The next attempt
            # opens a new one.
            self._forget(task_id, connection)
            raise

    # ---------- the sessions ----------
    async def _connection(self, task_id: str) -> _Connection:
        async with self._opening:
            found = self._connections.get(task_id)
            if found is not None and found.alive:
                self._connections.move_to_end(task_id)
                return found
            opened = _Connection(self._url, self._sign)
            self._connections[task_id] = opened
            while len(self._connections) > MAX_SESSIONS:
                _, oldest = self._connections.popitem(last=False)
                oldest.stop()
            return opened

    def _forget(self, task_id: str, connection: _Connection) -> None:
        if self._connections.get(task_id) is connection:
            del self._connections[task_id]
        connection.stop()

    async def aclose(self) -> None:
        """Close every session. The backend can be used again afterwards."""
        connections = list(self._connections.values())
        self._connections.clear()
        await asyncio.gather(*(c.close() for c in connections), return_exceptions=True)


# ---------- the mapping ----------
@dataclass(frozen=True)
class Reply:
    """What one answer of Keystone came to: the result, and whether it asks for a pause."""

    result: ToolResult
    question: str | None = None
    approval_id: str | None = None
    code: str | None = None  # the Keystone code of a refusal, None for a tool's own result


def interpret(sent: types.CallToolResult, call: ToolCall, duration_ms: int = 0) -> Reply:
    """Map the MCP result of a call to the result the model gets, and to a pause if one is due."""
    text = _text(sent)
    code = _code(sent, text) if sent.is_error else None
    if code == PENDING:
        approval_id = _meta_value(sent, APPROVAL_KEY)
        reason = _reason(text)
        shown = f" (request {approval_id})" if approval_id else ""
        question = f"Keystone holds {call.name}{shown} until a person approves it: {reason}"
        output = (
            f"not run: Keystone is holding this call{shown} until a person approves it. {reason}\n"
            f"Once a person has decided, call {call.name} again with the same arguments."
        )
        return Reply(_result(call, False, output, duration_ms), question, approval_id, code)
    exit_code = _exit_code(sent, call, text) if code is None else None
    return Reply(_result(call, not sent.is_error, text, duration_ms, exit_code), code=code)


def _fault(reply: Reply, call: ToolCall) -> KeystoneFault:
    reason = reply.result.output
    if reply.code == UPSTREAM_TIMEOUT:
        return KeystoneFault(
            f"Keystone gave up waiting for the worker's tool server on {call.name} "
            f"[{UPSTREAM_TIMEOUT}]: {reason} The command may have run, and the retry starts from "
            f"the checkpoint. If tests or installs are cut short, raise timeout_s for the tool in "
            f"the upstream's config in Keystone (docs/runbook.md)."
        )
    return KeystoneFault(
        f"Keystone could not get {call.name} run by the worker's tool server "
        f"[{UPSTREAM_ERROR}]: {reason}"
    )


def _unusable(result: ToolResult, call: ToolCall) -> ToolResult:
    """Say that a person's approval was lost, so the model does not take it for a plain denial."""
    note = (
        f"The approval of this call could not be used: its request expired, or the Keystone "
        f"session that made it is gone. The call did not run. Call {call.name} again to make a "
        f"new request, which a person has to approve again."
    )
    return result.model_copy(update={"output": f"{result.output}\n{note}"})


def _result(
    call: ToolCall, ok: bool, output: str, duration_ms: int, exit_code: int | None = None
) -> ToolResult:
    return ToolResult(
        call_id=call.id, ok=ok, output=output, exit_code=exit_code, duration_ms=duration_ms
    )


def _text(sent: types.CallToolResult) -> str:
    return "\n".join(b.text for b in sent.content if isinstance(b, types.TextContent))


def _code(sent: types.CallToolResult, text: str) -> str | None:
    """The Keystone code of a refusal: from the result's `_meta`, else the text block's, else the
    start of the text. Only an error result is read, so output that merely looks like a refusal
    is not taken for one."""
    found = _meta_value(sent, CODE_KEY)
    if found is not None:
        return found
    match = _REFUSAL.match(text)
    return match.group("code") if match else None


def _meta_value(sent: types.CallToolResult, key: str) -> str | None:
    metas = [sent.meta or {}]
    metas += [b.meta or {} for b in sent.content if isinstance(b, types.TextContent)]
    for meta in metas:
        if key in meta:
            return str(meta[key])
    return None


def _reason(text: str) -> str:
    match = _REFUSAL.match(text)
    return match.group("reason").strip() if match else text.strip()


def _exit_code(sent: types.CallToolResult, call: ToolCall, text: str) -> int | None:
    """The exit code of a command: the server sends it as structured content, and the first line
    of the output (tools/exec.py writes it there) is the fallback."""
    structured = sent.structured_content
    if isinstance(structured, dict) and isinstance(structured.get(EXIT_CODE_KEY), int):
        return int(structured[EXIT_CODE_KEY])
    if call.name not in _COMMAND_TOOLS:
        return None
    match = _EXIT_LINE.match(text)
    return int(match.group("code")) if match else None


def _context(
    ctx: ToolContext, sandbox_id: str, registry: ToolRegistry, call: ToolCall
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "task_id": ctx.task_id,
        "sandbox_id": sandbox_id,
        "turn_index": ctx.turn_index,
        "output_max_tokens": ctx.output_max_tokens,
    }
    # The server on the other side has the worker's registry. The registry of this call has the
    # command of the task, so a run_tests call names it (LH-991).
    tool = registry.get("run_tests")
    if call.name == "run_tests" and isinstance(tool, RunTests) and tool.test_command:
        context["test_command"] = tool.test_command
    return context


def _digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(arguments, sort_keys=True, default=str).encode()).hexdigest()


# ---------- from the settings ----------
def token_source(settings: EnvSettings) -> TokenSource:
    """Where the bearer token comes from: a file that is read for every request, else the
    environment. A file lets an operator rotate a token that expires without a restart."""
    if settings.keystone_token_file is not None:
        path: Path = settings.keystone_token_file
        return lambda: path.read_text(encoding="utf-8").strip() or None
    if settings.keystone_token is not None:
        value = settings.keystone_token.get_secret_value()
        return lambda: value
    raise ValueError(
        "tools.backend is keystone, so the worker needs a token to sign in to Keystone: "
        "set LONGHAUL_KEYSTONE_TOKEN or LONGHAUL_KEYSTONE_TOKEN_FILE"
    )


def build_backend(tools: Tools, settings: EnvSettings) -> ToolBackend:
    """The backend that `tools.backend` names. Keystone mode refuses to start without a token
    and without the key that protects the worker's own MCP server."""
    if tools.backend == "direct" or tools.gateway is None:
        return DirectBackend()
    token = token_source(settings)
    if settings.sandbox_mcp_key is None:
        raise ValueError(
            "tools.backend is keystone, so the worker serves its tools to Keystone and needs a "
            "key that protects them: set LONGHAUL_SANDBOX_MCP_KEY"
        )
    return KeystoneBackend(tools.gateway.url, upstream=tools.gateway.upstream, token=token)
