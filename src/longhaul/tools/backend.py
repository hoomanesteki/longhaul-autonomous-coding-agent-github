"""Where a tool call runs: in the sandbox directly, or through Keystone (LH-909).

`tool_exec` hands every call to a backend. The direct backend is the registry, as it always was.
The Keystone backend (keystone_backend.py) sends the call to a gateway that decides it first.
Everything else about a call stays in the activity: the start from the checkpoint, the stored
text, the `tool_calls` row and the checkpoint after it.

A backend answers with a `Ran`. Most of the time that is the tool's result and nothing more. A
gateway can also hold a call for a person, and then the result says the call did not run and
`approval_question` carries the reason, which the workflow turns into a pause (LH-506).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from longhaul.contracts import Sandbox, ToolCall, ToolContext, ToolResult
from longhaul.tools.registry import ToolRegistry


@dataclass(frozen=True)
class Ran:
    """What a backend made of one call."""

    result: ToolResult
    # Set when a gateway holds the call for a person. The text is the question to put to them.
    approval_question: str | None = None


class ToolBackend(Protocol):
    """Runs a tool call and says what came of it.

    `remote` is true when the call leaves the process. A remote call can fail in the sandbox and
    still come back as a result, so the activity looks for a vanished sandbox after a failed one.
    An error that is not the model's doing, such as an unreachable gateway, is raised and not
    returned, so Temporal runs the activity again.
    """

    remote: bool

    async def run(
        self,
        registry: ToolRegistry,
        sandbox: Sandbox,
        call: ToolCall,
        ctx: ToolContext,
        *,
        sandbox_id: str,
    ) -> Ran: ...

    async def aclose(self) -> None: ...


class DirectBackend:
    """Runs the call in the sandbox through the registry. This is the default."""

    remote = False

    async def run(
        self,
        registry: ToolRegistry,
        sandbox: Sandbox,
        call: ToolCall,
        ctx: ToolContext,
        *,
        sandbox_id: str,
    ) -> Ran:
        return Ran(await registry.run(call, sandbox, ctx))

    async def aclose(self) -> None:
        return None
