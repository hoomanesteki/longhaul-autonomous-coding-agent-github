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
- KEYSTONE_ADMIN_TOKEN: a token of an admin of the same tenant. With it the test also reads
  Keystone's audit log and checks that both calls are in it. Without it that half is skipped.
  KEYSTONE_ADMIN_URL is the admin API, by default Keystone's address with /admin/api/v1.

`finish` and `read_output` never leave the worker, so Keystone does not audit them.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest

from longhaul.contracts import Sandbox, ToolCall, ToolContext
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.backend import Ran
from longhaul.tools.keystone_backend import KeystoneBackend
from longhaul.tools.mcp_server import build_app, serving
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
AUDIT_WAIT_S = 15.0  # Keystone writes audit rows from a queue, so they land a moment later


def admin_url() -> str:
    explicit = os.environ.get("KEYSTONE_ADMIN_URL")
    if explicit:
        return explicit.rstrip("/")
    base = os.environ["KEYSTONE_URL"].rstrip("/")
    return base.removesuffix("/mcp") + "/admin/api/v1"


async def audit_rows(tool: str, wanted: int, token: str, needle: str) -> list[dict[str, Any]]:
    """The audit rows of `tool` whose arguments name `needle`, once there are `wanted` of them."""
    deadline = asyncio.get_running_loop().time() + AUDIT_WAIT_S
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        while True:
            reply = await http.get(
                f"{admin_url()}/audit", params={"tool": tool, "limit": 200}, timeout=10.0
            )
            reply.raise_for_status()
            rows = [r for r in reply.json()["items"] if needle in str(r.get("args_redacted"))]
            if len(rows) >= wanted or asyncio.get_running_loop().time() > deadline:
                return rows
            await asyncio.sleep(0.5)


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
    # A new id for every run, so the audit rows of this run are told from those of an earlier one.
    task_id = f"lh-keystone-check-{uuid.uuid4().hex[:8]}"
    ctx = ToolContext(task_id=task_id, turn_index=0, blob_prefix=f"tasks/{task_id}")
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

    # The other half of the criterion: Keystone has both calls in its audit log, tied to this run
    # by the task id the backend sends along and to one session.
    admin_token = os.environ.get("KEYSTONE_ADMIN_TOKEN")
    if not admin_token:
        pytest.skip("LH-909: set KEYSTONE_ADMIN_TOKEN to check Keystone's audit log as well")
    rows = await audit_rows("longhaul_sandbox__run_command", 2, admin_token, task_id)
    assert len(rows) == 2, f"expected the allowed and the denied call in the audit log: {rows}"
    assert len({row["session_id"] for row in rows}) == 1
    by_outcome = {row["reject_code"]: row for row in rows}
    assert by_outcome[None]["dispatched"] is True
    assert by_outcome["KS_DENIED"]["dispatched"] is False
