"""Live check that the prompt cache is hit as laid out in LH-405. It is paid, so it is opt-in.

    export ANTHROPIC_API_KEY=sk-ant-...
    uv run pytest -m live tests/live/test_cache_live.py

Without the key the test is skipped. CI has no key, and gates, `make itest` and `make e2e` leave
live tests out. The key is read from the environment only.

The request is the one a coder task sends: the real system prompt (blocks 1 to 4), the real tool
list and a growing conversation. The conversation is made up, so the test does not depend on what
the model decides to do. Four requests cost a few cents at most on the cheapest model in
config/longhaul.example.yaml. The test also needs the prefix to be over the model's minimum
cacheable length (512 tokens on the 5.5 models). A shorter prefix is never cached and the API says
nothing, which would make a failure here look like a layout bug.
"""

import json
import os
from pathlib import Path

import pytest

from longhaul.agent.prompts import FIRST_USER_MESSAGE, build_system_prompt
from longhaul.context.tokens import cache_ratio, estimate_tokens
from longhaul.contracts import (
    CallOptions,
    Message,
    RepoSettings,
    Role,
    TaskSpec,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from longhaul.providers.anthropic import AnthropicProvider
from longhaul.settings import load_config
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.live, pytest.mark.phase4]

CONFIG = Path(__file__).resolve().parents[2] / "config" / "longhaul.example.yaml"
MIN_CACHEABLE_TOKENS = 512
TURNS = 4
SPEC = TaskSpec(
    task_id="lh-acme-calc-42",
    repo="acme/calc",
    issue_number=42,
    title="add() returns the difference",
    body="`calc.add` subtracts its arguments instead of adding them.",
    comments=[],
    base_branch="main",
    settings=RepoSettings(repo="acme/calc", test_command="pytest -q"),
)
FILE = "\n".join(f"{n:4d}  def helper_{n}(a, b):  # returns a + b" for n in range(1, 41))


def conversation(turn: int) -> list[Message]:
    """The transcript of request `turn` (1-based): the first message and `turn - 1` tool rounds.

    Each result is large enough that the conversation, not only the system prompt, is worth
    caching. A fresh path in every round keeps one round from being a copy of the last.
    """
    messages = [Message(role="user", content=[TextBlock(text=FIRST_USER_MESSAGE)])]
    for n in range(1, turn):
        call_id = f"toolu_{n:024d}"
        messages += [
            Message(
                role="assistant",
                content=[
                    TextBlock(text=f"I will read module {n}."),
                    ToolUseBlock(id=call_id, name="read_file", input={"path": f"src/calc/m{n}.py"}),
                ],
            ),
            Message(role="user", content=[ToolResultBlock(tool_use_id=call_id, content=FILE)]),
        ]
    return messages


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="LH-405: set ANTHROPIC_API_KEY to run the live cache check (it is paid)",
)
async def test_cache_hits_after_turn_2() -> None:
    config = load_config(CONFIG)
    model = config.models.compactor
    options = CallOptions(effort=config.effort.compactor)
    provider = AnthropicProvider(os.environ["ANTHROPIC_API_KEY"])
    system = build_system_prompt(Role.CODER, SPEC)
    tools = default_registry().specs()
    # The prefix is the system prompt and the tools. Characters are a rough count, but a prefix
    # of twice the minimum is clear of it whichever way the estimate is off.
    prefix = system + json.dumps([tool.model_dump() for tool in tools])
    assert estimate_tokens(prefix) > 2 * MIN_CACHEABLE_TOKENS

    usages = []
    for turn in range(1, TURNS + 1):
        reply = await provider.complete(
            model=model,
            system=system,
            messages=conversation(turn),
            tools=tools,
            max_tokens=512,
            options=options,
        )
        usages.append(reply.usage)

    # Request 1 has only the prefix to cache. It writes it, or reads it when an earlier run of
    # this test wrote the same prefix less than five minutes ago. Either way it is the prefix.
    prefix_tokens = usages[0].cache_write_tokens + usages[0].cache_read_tokens
    assert prefix_tokens > 0, usages
    # From request 2 on the prefix is read. Requests 3 and up also read the earlier rounds of the
    # conversation, so they read more than the prefix alone.
    for usage in usages[1:]:
        assert usage.cache_read_tokens >= prefix_tokens, usages
    for usage in usages[2:]:
        assert usage.cache_read_tokens > prefix_tokens, usages
    assert cache_ratio(usages[-1]) > cache_ratio(usages[1])
