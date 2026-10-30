"""Control tools: the model can call them, the agent loop handles them.

`finish` ends a task. The loop reads the summary from the tool call itself and
stops, so nothing runs in the sandbox for it. If a call is still sent through
the registry, for example to get a tool_result that pairs with the tool_use,
`run` returns an ok result that says the loop handles it. A result is used
rather than an error so that pairing needs no special case in the loop.
"""

from __future__ import annotations

from typing import Any

from longhaul.contracts import Sandbox, ToolContext, ToolResult, ToolSpec
from longhaul.tools.files import ok

FINISH = "finish"
CONTROL_TOOL_NAMES: frozenset[str] = frozenset({FINISH})
HANDLED_BY_LOOP = "finish is handled by the agent loop. Nothing was run."


def is_control_tool(name: str) -> bool:
    """True for tools the loop must handle itself instead of running them."""
    return name in CONTROL_TOOL_NAMES


class Finish:
    spec = ToolSpec(
        name=FINISH,
        description=(
            "End the task. Call it only when the tests pass, "
            "with a short summary of what you changed."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "What you changed and why, in a few sentences.",
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    )
    mutates = False

    async def run(self, sandbox: Sandbox, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # The sandbox argument is deliberately unused.
        return ok(HANDLED_BY_LOOP)
