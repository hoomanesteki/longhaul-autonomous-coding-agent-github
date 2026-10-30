"""OpenAI-compatible provider: request conversion, response mapping, errors.

The openai 3.x SDK sends requests through httpx2, so the mock transport comes
from httpx2 too. No network is used.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from longhaul.contracts import (
    Message,
    ProviderError,
    ProviderErrorKind,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from longhaul.providers.base import get_provider
from longhaul.providers.openai_compat import OpenAICompatProvider
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

BASE_URL = "https://api.test/v1"
Handler = Callable[[httpx2.Request], httpx2.Response]


def _provider(handler: Handler) -> OpenAICompatProvider:
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAICompatProvider(api_key="sk-test", base_url=BASE_URL, http_client=http_client)


def _completion(
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-test",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


READ_FILE = ToolSpec(
    name="read_file",
    description="Read a file",
    input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
)


async def test_tool_roundtrip() -> None:
    transcript = [
        Message(role="user", content=[TextBlock(text="fix it")]),
        Message(
            role="assistant",
            content=[
                TextBlock(text="Looking."),
                ToolUseBlock(id="call_1", name="read_file", input={"path": "a.py"}),
                ToolUseBlock(id="call_2", name="read_file", input={"path": "b.py"}),
            ],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock(tool_use_id="call_1", content="print(1)"),
                ToolResultBlock(tool_use_id="call_2", content="no such file", is_error=True),
                TextBlock(text="next"),
            ],
        ),
    ]
    seen: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/chat/completions"
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json=_completion(
                content="Let me check.",
                tool_calls=[
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "c.py"}'},
                    }
                ],
                finish_reason="tool_calls",
                usage={
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 64},
                },
            ),
        )

    resp = await _provider(handler).complete(
        model="gpt-test",
        system="You are a coder.",
        messages=transcript,
        tools=[READ_FILE],
        max_tokens=256,
    )

    body = seen[0]
    assert body["model"] == "gpt-test"
    assert body["max_tokens"] == 256
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": READ_FILE.input_schema,
            },
        }
    ]
    assert body["messages"] == [
        {"role": "system", "content": "You are a coder."},
        {"role": "user", "content": "fix it"},
        {
            "role": "assistant",
            "content": "Looking.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "b.py"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "print(1)"},
        {"role": "tool", "tool_call_id": "call_2", "content": "ERROR: no such file"},
        {"role": "user", "content": "next"},
    ]

    assert resp.stop_reason == "tool_use"
    assert resp.tool_calls == [ToolCall(id="call_9", name="read_file", input={"path": "c.py"})]
    assert resp.content == [
        TextBlock(text="Let me check."),
        ToolUseBlock(id="call_9", name="read_file", input={"path": "c.py"}),
    ]
    assert resp.usage.input_tokens == 120
    assert resp.usage.output_tokens == 30
    assert resp.usage.cache_read_tokens == 64
    assert resp.model == "gpt-test"
    assert resp.latency_ms >= 0


_RATE_LIMIT_BODY = {"error": {"message": "slow down", "type": "rate_limit_error"}}
_CONTEXT_BODY = {
    "error": {
        "message": "This model's maximum context length is 128000 tokens.",
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
    }
}


def _status(status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=body, headers=headers)

    return handler


def _timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("read timed out")


@pytest.mark.parametrize(
    ("handler", "kind", "retry_after_s"),
    [
        pytest.param(
            _status(429, _RATE_LIMIT_BODY, {"retry-after": "7"}),
            ProviderErrorKind.RATE_LIMIT,
            7.0,
            id="429",
        ),
        pytest.param(
            _status(500, {"error": {"message": "internal"}}),
            ProviderErrorKind.SERVER,
            None,
            id="500",
        ),
        pytest.param(
            _status(503, {"error": {"message": "Service overloaded"}}),
            ProviderErrorKind.OVERLOADED,
            None,
            id="503-overloaded",
        ),
        pytest.param(
            _status(529, {"error": {"message": "overloaded"}}),
            ProviderErrorKind.OVERLOADED,
            None,
            id="529",
        ),
        pytest.param(
            _status(401, {"error": {"message": "bad key"}}),
            ProviderErrorKind.AUTH,
            None,
            id="401",
        ),
        pytest.param(
            _status(400, _CONTEXT_BODY),
            ProviderErrorKind.CONTEXT_TOO_LONG,
            None,
            id="400-context",
        ),
        pytest.param(
            _status(400, {"error": {"message": "bad param", "code": "invalid_value"}}),
            ProviderErrorKind.INVALID_REQUEST,
            None,
            id="400-other",
        ),
        pytest.param(_timeout, ProviderErrorKind.TIMEOUT, None, id="timeout"),
    ],
)
async def test_error_mapping(
    handler: Handler, kind: ProviderErrorKind, retry_after_s: float | None
) -> None:
    provider = _provider(handler)
    with pytest.raises(ProviderError) as info:
        await provider.complete(
            model="gpt-test",
            system="sys",
            messages=[Message(role="user", content=[TextBlock(text="hi")])],
            tools=[],
            max_tokens=10,
        )
    assert info.value.kind == kind
    assert info.value.retry_after_s == retry_after_s
    assert info.value.retryable == (
        kind
        in {
            ProviderErrorKind.RATE_LIMIT,
            ProviderErrorKind.OVERLOADED,
            ProviderErrorKind.SERVER,
            ProviderErrorKind.TIMEOUT,
        }
    )


async def test_malformed_arguments() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        call = {
            "id": "call_bad",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{not json"},
        }
        return httpx2.Response(200, json=_completion(tool_calls=[call], finish_reason="tool_calls"))

    resp = await _provider(handler).complete(
        model="gpt-test",
        system="sys",
        messages=[Message(role="user", content=[TextBlock(text="go")])],
        tools=[READ_FILE],
        max_tokens=10,
    )
    assert resp.tool_calls == [
        ToolCall(id="call_bad", name="read_file", input={"_raw": "{not json"})
    ]
    assert resp.stop_reason == "tool_use"


def test_get_provider_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        get_provider(EnvSettings(provider="openai"))

    settings = EnvSettings(provider="openai", OPENAI_API_KEY=SecretStr("sk-test"))  # type: ignore[call-arg]
    provider = get_provider(settings)
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.name == "openai"
