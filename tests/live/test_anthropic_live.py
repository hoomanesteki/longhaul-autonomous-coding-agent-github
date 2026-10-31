"""Live check of AnthropicProvider against the real API (LH-104). It is paid, so it is opt-in.

    export ANTHROPIC_API_KEY=sk-ant-...
    uv run pytest -m live tests/live/test_anthropic_live.py

Without the key the test is skipped. CI has no key, and gates, `make itest` and `make e2e`
leave live tests out. The key is read from the environment only; a .env file is not read here.

One round trip costs well under a cent. It uses the cheapest model in
config/longhaul.example.yaml at the effort that file gives it.
"""

import os
from pathlib import Path

import pytest

from longhaul.contracts import (
    CallOptions,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    ToolSpec,
)
from longhaul.providers.anthropic import AnthropicProvider
from longhaul.settings import load_config

pytestmark = [pytest.mark.live, pytest.mark.phase1]

CONFIG = Path(__file__).resolve().parents[2] / "config" / "longhaul.example.yaml"
SYSTEM = "You are a calculator. Use the tools you are given. Never do arithmetic yourself."
# strict, so the live call also checks that the API takes the flag the provider renders.
ADD = ToolSpec(
    name="add",
    description="Add two integers and return the sum.",
    input_schema={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
    strict=True,
)


def _text(response: LLMResponse) -> str:
    return " ".join(b.text for b in response.content if isinstance(b, TextBlock))


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="LH-104: set ANTHROPIC_API_KEY to run the live Anthropic round trip (it is paid)",
)
async def test_round_trip() -> None:
    config = load_config(CONFIG)
    model = config.models.compactor
    options = CallOptions(effort=config.effort.compactor)
    provider = AnthropicProvider(os.environ["ANTHROPIC_API_KEY"])
    asked = [Message(role="user", content=[TextBlock(text="Add 17 and 25 with the add tool.")])]

    # Turn 1, a plain call: the model asks for the tool.
    first = await provider.complete(
        model=model, system=SYSTEM, messages=asked, tools=[ADD], max_tokens=2048, options=options
    )

    assert first.stop_reason == "tool_use", first
    assert [call.name for call in first.tool_calls] == ["add"]
    call = first.tool_calls[0]
    assert call.id.startswith("toolu_")
    assert sorted(call.input.values()) == [17, 25]
    assert first.request_id is not None and first.request_id.startswith("req_")
    assert first.model == model
    assert first.usage.output_tokens > 0
    prompt_tokens = (
        first.usage.input_tokens + first.usage.cache_read_tokens + first.usage.cache_write_tokens
    )
    assert prompt_tokens > 0

    # Turn 2 sends the result back with every thinking block stripped (ADR-0003, D1), so this
    # also checks that the API accepts that loop. The cap is only a ceiling, but it is above
    # 16000, so this turn goes over the streaming path.
    transcript = [
        *asked,
        Message(role="assistant", content=first.content),
        Message(role="user", content=[ToolResultBlock(tool_use_id=call.id, content="42")]),
    ]
    second = await provider.complete(
        model=model,
        system=SYSTEM,
        messages=transcript,
        tools=[ADD],
        max_tokens=20000,
        options=options,
    )

    assert second.stop_reason == "end_turn", second
    assert second.tool_calls == []
    assert "42" in _text(second)
    assert second.request_id is not None and second.request_id != first.request_id

    # Counting is free, and it reads the same system, tools and options.
    counted = await provider.count_tokens(
        model=model, system=SYSTEM, messages=transcript, tools=[ADD], options=options
    )
    assert counted > 0
