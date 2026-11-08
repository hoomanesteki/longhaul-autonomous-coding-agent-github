"""Tool calls through a live Keystone (LH-909).

This test needs a Keystone gateway that someone set up for it, so it runs only when KEYSTONE_URL
is set and is skipped otherwise. docs/runbook.md ("Route tool calls through Keystone") lists what
to set up: the `longhaul_sandbox` upstream pointing at the server this test starts, a policy that
denies `run_command` when the command contains `curl`, a token for an agent that may call the
upstream, and the key that Keystone sends to the worker's server.

Settings, all from the environment:

- KEYSTONE_URL: Keystone's MCP endpoint, for example http://localhost:8080/mcp.
- KEYSTONE_TOKEN: an access token for an agent of the tenant that owns the upstream.
- LONGHAUL_SANDBOX_MCP_KEY: the key the upstream is registered with (credential header
  X-Api-Key). The test's server accepts only a request that carries it.
- KEYSTONE_UPSTREAM_HOST and KEYSTONE_UPSTREAM_PORT: where the test serves the upstream. They
  default to 127.0.0.1 and 8765. Keystone in Docker reaches this machine on host.docker.internal,
  so set the host to 0.0.0.0 then.
- KEYSTONE_UPSTREAM_ID: the id the upstream has in Keystone. Default longhaul_sandbox.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from longhaul.tools.backend import Ran
from longhaul.tools.keystone_backend import KeystoneBackend
from longhaul.tools.mcp_server import build_app, serving

from longhaul.contracts import Sandbox, ToolCall, ToolContext
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.keystone,
    pytest.mark.phase9,
    pytest.mark.skipif(
        not os.environ.get("KEYSTONE_URL"),
        reason="LH-909: set KEYSTONE_URL (and see docs/runbook.md) to run this against a Keystone",
    ),
]

MARKER = "denied-marker"


class Boxes:
    def __init__(self, box: FakeSandbox) -> None:
        self.box = box

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.box if sandbox_id == self.box.id else None


@pytest.fixture
async def sandbox() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox("sbx-keystone")
    yield box
    await box.destroy()


@pytest.fixture
async def backend(sandbox: FakeSandbox) -> AsyncIterator[KeystoneBackend]:
    key = os.environ["LONGHAUL_SANDBOX_MCP_KEY"]
    host = os.environ.get("KEYSTONE_UPSTREAM_HOST", "127.0.0.1")
    port = int(os.environ.get("KEYSTONE_UPSTREAM_PORT", "8765"))
    app = build_app(default_registry(), Boxes(sandbox), key=key)
    found = KeystoneBackend(
        os.environ["KEYSTONE_URL"],
        upstream=os.environ.get("KEYSTONE_UPSTREAM_ID", "longhaul_sandbox"),
        token=lambda: os.environ["KEYSTONE_TOKEN"],
    )
    async with serving(app, host, port):
        yield found
    await found.aclose()


async def test_denied_command_surfaces(backend: KeystoneBackend, sandbox: FakeSandbox) -> None:
    ctx = ToolContext(task_id="lh-keystone-check", turn_index=0, blob_prefix="tasks/lh-keystone")
    registry = default_registry()

    async def call(call_id: str, command: str) -> Ran:
        tool_call = ToolCall(id=call_id, name="run_command", input={"command": command})
        return await backend.run(registry, sandbox, tool_call, ctx, sandbox_id=sandbox.id)

    allowed = await call("c1", "echo allowed")
    denied = await call("c2", f"curl http://example.com; touch {MARKER}")

    assert allowed.result.ok
    assert "allowed" in allowed.result.output
    # The agent sees a failed result that says why, and Keystone never let the command run.
    assert not denied.result.ok
    assert "KS_DENIED" in denied.result.output
    assert not (sandbox.root / MARKER).exists()
