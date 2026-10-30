"""OpenAI-compatible provider: request conversion, response mapping, errors.

The openai 3.x SDK sends requests through httpx2, so the mock transport comes
from httpx2 too. No network is used.
"""

import email.utils
import json
import time
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import SecretStr

from longhaul.contracts import (
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from longhaul.providers.base import get_provider
from longhaul.providers.openai_compat import OpenAICompatProvider
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

BASE_URL = "https://api.test/v1"
Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    """Wraps a handler and keeps every request, so tests can inspect and count them."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._handler(request)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def _provider(
    handler: Handler, *, base_url: str | None = BASE_URL, **options: Any
) -> OpenAICompatProvider:
    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAICompatProvider(
        api_key="sk-test", base_url=base_url, http_client=http_client, **options
    )


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
HI = [Message(role="user", content=[TextBlock(text="hi")])]


async def _ask(
    provider: OpenAICompatProvider,
    *,
    messages: list[Message] | None = None,
    tools: list[ToolSpec] | None = None,
    max_tokens: int = 10,
) -> LLMResponse:
    return await provider.complete(
        model="gpt-test",
        system="sys",
        messages=messages or HI,
        tools=tools or [],
        max_tokens=max_tokens,
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
    # prompt_tokens (120) already counts the 64 cached tokens. Usage keeps the
    # buckets apart, like the Anthropic usage the contract follows.
    assert resp.usage.input_tokens == 56
    assert resp.usage.output_tokens == 30
    assert resp.usage.cache_read_tokens == 64
    assert resp.usage.cache_write_tokens == 0
    assert resp.model == "gpt-test"
    assert resp.latency_ms >= 0


def _usage_body(prompt: int, details: dict[str, Any] | None) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": 7,
        "total_tokens": prompt + 7,
    }
    if details is not None:
        usage["prompt_tokens_details"] = details
    return _completion(content="ok", usage=usage)


@pytest.mark.parametrize(
    ("prompt", "details", "input_tokens", "cache_read_tokens"),
    [
        pytest.param(100_000, {"cached_tokens": 90_000}, 10_000, 90_000, id="mostly-cached"),
        pytest.param(120, {"cached_tokens": 64}, 56, 64, id="partly-cached"),
        pytest.param(50, {"cached_tokens": 0}, 50, 0, id="nothing-cached"),
        pytest.param(50, {"cached_tokens": 50}, 0, 50, id="fully-cached"),
        pytest.param(50, {"cached_tokens": None}, 50, 0, id="cached-null"),
        pytest.param(50, None, 50, 0, id="no-details"),
        # A server that over-reports cached tokens must not make input negative.
        pytest.param(50, {"cached_tokens": 80}, 0, 80, id="cached-above-prompt"),
    ],
)
async def test_usage_input_excludes_cached_tokens(
    prompt: int, details: dict[str, Any] | None, input_tokens: int, cache_read_tokens: int
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_usage_body(prompt, details))

    usage = (await _ask(_provider(handler))).usage

    assert usage.input_tokens == input_tokens
    assert usage.cache_read_tokens == cache_read_tokens
    assert usage.cache_write_tokens == 0
    assert usage.output_tokens == 7


async def test_usage_buckets_add_up_to_the_prompt() -> None:
    # LH-405 reads cache_read / (input + cache_read) and LH-701 prices the three
    # buckets separately, so they must not overlap.
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_usage_body(100_000, {"cached_tokens": 90_000}))

    usage = (await _ask(_provider(handler))).usage

    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens == 100_000
    assert usage.cache_read_tokens / (usage.input_tokens + usage.cache_read_tokens) == 0.9


async def test_missing_usage_is_zero() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        body = _completion(content="ok")
        del body["usage"]
        return httpx2.Response(200, json=body)

    assert (await _ask(_provider(handler))).usage == Usage()


def _ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=_completion(content="ok"))


@pytest.mark.parametrize(
    ("base_url", "options", "host", "name", "other"),
    [
        pytest.param(
            None, {}, "api.openai.com", "max_completion_tokens", "max_tokens", id="official"
        ),
        pytest.param(
            "https://api.openai.com/v1",
            {},
            "api.openai.com",
            "max_completion_tokens",
            "max_tokens",
            id="official-explicit-url",
        ),
        pytest.param(BASE_URL, {}, "api.test", "max_tokens", "max_completion_tokens", id="other"),
        pytest.param(
            BASE_URL,
            {"max_tokens_param": "max_completion_tokens"},
            "api.test",
            "max_completion_tokens",
            "max_tokens",
            id="other-opt-in",
        ),
        pytest.param(
            None,
            {"max_tokens_param": "max_tokens"},
            "api.openai.com",
            "max_tokens",
            "max_completion_tokens",
            id="official-opt-out",
        ),
    ],
)
async def test_max_tokens_parameter_name(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str | None,
    options: dict[str, Any],
    host: str,
    name: str,
    other: str,
) -> None:
    # OpenAI's reasoning models reject max_tokens. Other servers mostly know only that name.
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    seen = Recorder(_ok)

    await _ask(_provider(seen, base_url=base_url, **options), max_tokens=321)

    assert seen.requests[0].url.host == host
    assert seen.bodies[0][name] == 321
    assert other not in seen.bodies[0]


@pytest.mark.parametrize(
    ("base_url", "name"),
    [
        ("http://localhost:8000/v1", "max_tokens"),
        ("https://api.openai.com/v1", "max_completion_tokens"),
    ],
)
async def test_max_tokens_parameter_follows_a_prebuilt_client(base_url: str, name: str) -> None:
    seen = Recorder(_ok)
    client = AsyncOpenAI(
        api_key="sk-test",
        base_url=base_url,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(seen)),
        max_retries=0,
    )

    await _ask(OpenAICompatProvider(api_key="sk-test", client=client), max_tokens=64)

    assert seen.bodies[0][name] == 64


def test_max_tokens_parameter_name_is_checked() -> None:
    with pytest.raises(ValueError, match="max_tokens_param"):
        OpenAICompatProvider(api_key="sk-test", max_tokens_param="max_new_tokens")  # type: ignore[arg-type]


_RATE_LIMIT_BODY = {"error": {"message": "slow down", "type": "rate_limit_error"}}
_CONTEXT_BODY = {
    "error": {
        "message": "This model's maximum context length is 128000 tokens.",
        "type": "invalid_request_error",
        "code": "context_length_exceeded",
    }
}
# OpenAI answers 429 for three different things. Only the first is worth retrying.
_TPM_BODY = {
    "error": {
        "message": (
            "Rate limit reached for gpt-4o in organization org-x on tokens per min (TPM): "
            "Limit 30000, Used 29000, Requested 2000. Please try again in 2s."
        ),
        "type": "tokens",
        "param": None,
        "code": "rate_limit_exceeded",
    }
}
_QUOTA_BODY = {
    "error": {
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "type": "insufficient_quota",
        "param": None,
        "code": "insufficient_quota",
    }
}
_TOO_LARGE_BODY = {
    "error": {
        "message": (
            "Request too large for gpt-4o in organization org-x on tokens per min (TPM): "
            "Limit 30000, Requested 36000. The input or output tokens must be reduced in "
            "order to run successfully."
        ),
        "type": "tokens",
        "param": None,
        "code": "rate_limit_exceeded",
    }
}


def _status(status: int, body: Any, headers: dict[str, str] | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=body, headers=headers)

    return handler


def _raw(status: int, content: bytes, headers: dict[str, str] | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, content=content, headers=headers)

    return handler


def _timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("read timed out")


def _connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused")


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
        # The commonest transient failure. It must stay retryable.
        pytest.param(_connect_error, ProviderErrorKind.SERVER, None, id="connect-error"),
        # The SDK retries 408 and 409 itself when its retries are on. With them
        # off, this mapping is the only place that keeps them transient.
        pytest.param(
            _status(408, {"error": {"message": "Request timed out"}}),
            ProviderErrorKind.TIMEOUT,
            None,
            id="408",
        ),
        pytest.param(
            _status(409, {"error": {"message": "lock timeout"}}),
            ProviderErrorKind.SERVER,
            None,
            id="409",
        ),
        pytest.param(
            _status(403, {"error": {"message": "forbidden"}}),
            ProviderErrorKind.AUTH,
            None,
            id="403",
        ),
        pytest.param(
            _status(404, {"error": {"message": "no such model", "code": "model_not_found"}}),
            ProviderErrorKind.INVALID_REQUEST,
            None,
            id="404",
        ),
        # A plain tokens-per-minute 429 clears with time. These two never will.
        pytest.param(
            _status(429, _TPM_BODY), ProviderErrorKind.RATE_LIMIT, None, id="429-tpm-limit"
        ),
        pytest.param(
            _status(429, _QUOTA_BODY), ProviderErrorKind.AUTH, None, id="429-insufficient-quota"
        ),
        pytest.param(
            _status(429, _TOO_LARGE_BODY),
            ProviderErrorKind.CONTEXT_TOO_LONG,
            None,
            id="429-request-too-large",
        ),
    ],
)
async def test_error_mapping(
    handler: Handler, kind: ProviderErrorKind, retry_after_s: float | None
) -> None:
    seen = Recorder(handler)
    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen))
    # One transport call per attempt: the SDK must not retry. Temporal owns
    # retries (LH-104), and SDK retries would repeat paid calls the cost ledger
    # never sees.
    assert len(seen.requests) == 1
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


_VLLM_BODY = {
    "object": "error",
    "message": (
        "This model's maximum context length is 4096 tokens. However, you requested 5000 "
        "tokens (4500 in the messages, 500 in the completion)."
    ),
    "type": "BadRequestError",
    "param": None,
    "code": 400,
}
_LLAMA_CPP_BODY = {
    "error": {
        "code": 400,
        "message": "the request exceeds the available context size, try increasing it",
        "type": "exceed_context_size_error",
        "n_prompt_tokens": 9000,
        "n_ctx": 4096,
    }
}
_TGI_BODY = {
    "error": (
        "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 8192. "
        "Given: 9000 `inputs` tokens and 512 `max_new_tokens`"
    ),
    "error_type": "validation",
}
_GEMINI_BODY = [
    {
        "error": {
            "code": 400,
            "message": (
                "The input token count (1200000) exceeds the maximum number of tokens "
                "allowed (1048576)."
            ),
            "status": "INVALID_ARGUMENT",
        }
    }
]


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(
            _status(400, {"error": {"message": "bad", "code": "context_length_exceeded"}}),
            id="openai-code-only",
        ),
        pytest.param(
            _status(
                400,
                {"error": {"message": "Your input exceeds the context window of this model."}},
            ),
            id="openai-window-wording",
        ),
        pytest.param(
            _status(400, {"error": {"message": "This model's maximum context length is 8192."}}),
            id="openai-length-wording",
        ),
        pytest.param(_status(400, _LLAMA_CPP_BODY), id="llama-cpp"),
        pytest.param(
            _status(400, {"error": {"message": "too big", "type": "exceed_context_size_error"}}),
            id="llama-cpp-type-only",
        ),
        pytest.param(_status(400, _VLLM_BODY), id="vllm-flat-body"),
        pytest.param(_status(422, _TGI_BODY), id="tgi-error-is-a-string"),
        pytest.param(_status(400, _GEMINI_BODY), id="gemini-list-body"),
        pytest.param(
            _raw(400, b"prompt is too long: 250000 tokens > 200000 maximum"),
            id="plain-text-body",
        ),
        pytest.param(
            _raw(
                413,
                b"<html><body><h1>413 Request Entity Too Large</h1></body></html>",
                {"content-type": "text/html"},
            ),
            id="413-html",
        ),
        pytest.param(_status(413, {"error": {"message": "payload too large"}}), id="413-json"),
    ],
)
async def test_context_overflow_wordings(handler: Handler) -> None:
    # Servers word this differently, and the SDK copies only error.message into
    # exc.message. F6 compaction runs only when the kind says the input is too long.
    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))
    assert info.value.kind == ProviderErrorKind.CONTEXT_TOO_LONG
    assert not info.value.retryable


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(
            _status(400, {"error": {"message": "Invalid value for 'context': expected an object"}}),
            id="context-field",
        ),
        pytest.param(
            _status(
                400,
                {
                    "error": {
                        "message": "Unsupported parameter: 'max_tokens' is not supported with this "
                        "model. Use 'max_completion_tokens' instead.",
                        "type": "invalid_request_error",
                        "param": "max_tokens",
                        "code": "unsupported_parameter",
                    }
                },
            ),
            id="unsupported-max-tokens",
        ),
        pytest.param(
            _raw(400, b"<html>bad request</html>", {"content-type": "text/html"}),
            id="html-body",
        ),
    ],
)
async def test_other_client_errors_stay_invalid_request(handler: Handler) -> None:
    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))
    assert info.value.kind == ProviderErrorKind.INVALID_REQUEST
    assert not info.value.retryable


@pytest.mark.parametrize(
    ("handler", "kind"),
    [
        pytest.param(
            _status(400, {"error": {"message": "try again"}}, {"x-should-retry": "true"}),
            ProviderErrorKind.SERVER,
            id="true-on-400",
        ),
        pytest.param(
            _status(500, {"error": {"message": "broken"}}, {"x-should-retry": "true"}),
            ProviderErrorKind.SERVER,
            id="true-on-500",
        ),
        pytest.param(
            _status(500, {"error": {"message": "broken"}}, {"x-should-retry": "false"}),
            ProviderErrorKind.INVALID_REQUEST,
            id="false-on-500",
        ),
        pytest.param(
            _status(429, _RATE_LIMIT_BODY, {"x-should-retry": "false"}),
            ProviderErrorKind.INVALID_REQUEST,
            id="false-on-429",
        ),
        pytest.param(
            _status(529, {"error": {"message": "overloaded"}}, {"x-should-retry": "false"}),
            ProviderErrorKind.INVALID_REQUEST,
            id="false-on-529",
        ),
        pytest.param(
            _status(408, {"error": {"message": "slow"}}, {"x-should-retry": "false"}),
            ProviderErrorKind.INVALID_REQUEST,
            id="false-on-408",
        ),
        # The hint only moves generic outcomes. A bad key or an oversized prompt
        # does not become retryable because a gateway says so.
        pytest.param(
            _status(401, {"error": {"message": "bad key"}}, {"x-should-retry": "true"}),
            ProviderErrorKind.AUTH,
            id="true-keeps-auth",
        ),
        pytest.param(
            _status(400, _CONTEXT_BODY, {"x-should-retry": "true"}),
            ProviderErrorKind.CONTEXT_TOO_LONG,
            id="true-keeps-context",
        ),
        pytest.param(
            _status(400, {"error": {"message": "bad"}}, {"x-should-retry": "maybe"}),
            ProviderErrorKind.INVALID_REQUEST,
            id="unknown-value-ignored",
        ),
    ],
)
async def test_should_retry_header_is_honoured(handler: Handler, kind: ProviderErrorKind) -> None:
    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))
    assert info.value.kind == kind


async def _error(handler: Handler) -> ProviderError:
    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))
    return info.value


_OVERLOADED_BODY = {"error": {"message": "Service overloaded"}}


@pytest.mark.parametrize(
    ("status", "body", "headers", "retry_after_s"),
    [
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "7"}, 7.0, id="seconds"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "0.5"}, 0.5, id="fractional-seconds"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after-ms": "1500"}, 1.5, id="milliseconds"),
        pytest.param(
            429,
            _RATE_LIMIT_BODY,
            {"retry-after-ms": "2500", "retry-after": "9"},
            2.5,
            id="milliseconds-first",
        ),
        # Any transient status can carry the hint, not only 429.
        pytest.param(503, _OVERLOADED_BODY, {"retry-after": "30"}, 30.0, id="503-overloaded"),
        pytest.param(529, _OVERLOADED_BODY, {"retry-after": "30"}, 30.0, id="529"),
        pytest.param(500, {"error": {"message": "x"}}, {"retry-after": "5"}, 5.0, id="500"),
        pytest.param(408, {"error": {"message": "x"}}, {"retry-after": "3"}, 3.0, id="408"),
        # Values the durable layer cannot use leave it to its own backoff. A nan
        # or inf breaks timedelta(seconds=...), and a negative value gives a past retry.
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "nan"}, None, id="nan"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "inf"}, None, id="inf"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "1e999"}, None, id="overflow"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "-5"}, None, id="negative"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "0"}, None, id="zero"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "soon"}, None, id="not-a-number"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "1e300"}, None, id="absurdly-long"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "86400"}, 86400.0, id="one-day"),
        pytest.param(429, _RATE_LIMIT_BODY, {"retry-after": "86401"}, None, id="over-a-day"),
        # A bad first form falls through to the next one.
        pytest.param(
            429,
            _RATE_LIMIT_BODY,
            {"retry-after-ms": "nan", "retry-after": "9"},
            9.0,
            id="bad-milliseconds-then-seconds",
        ),
        pytest.param(
            429,
            _RATE_LIMIT_BODY,
            {"retry-after-ms": "-100", "retry-after": "9"},
            9.0,
            id="negative-milliseconds-then-seconds",
        ),
        # A delay is only a hint for errors that will be retried.
        pytest.param(401, {"error": {"message": "x"}}, {"retry-after": "5"}, None, id="401"),
        pytest.param(400, {"error": {"message": "x"}}, {"retry-after": "5"}, None, id="400"),
        pytest.param(429, _QUOTA_BODY, {"retry-after": "5"}, None, id="429-insufficient-quota"),
    ],
)
async def test_retry_after_forms(
    status: int, body: dict[str, Any], headers: dict[str, str], retry_after_s: float | None
) -> None:
    error = await _error(_status(status, body, headers))
    assert error.retry_after_s == retry_after_s


async def test_retry_after_http_date() -> None:
    when = email.utils.formatdate(time.time() + 30, usegmt=True)
    error = await _error(_status(429, _RATE_LIMIT_BODY, {"retry-after": when}))
    # The date has whole-second resolution, so allow for rounding and a slow runner.
    assert error.retry_after_s is not None
    assert 20 < error.retry_after_s <= 30


async def test_retry_after_http_date_in_the_past_is_ignored() -> None:
    when = email.utils.formatdate(time.time() - 60, usegmt=True)
    error = await _error(_status(429, _RATE_LIMIT_BODY, {"retry-after": when}))
    assert error.retry_after_s is None


async def test_prebuilt_client_gets_no_sdk_retries() -> None:
    # An SDK client built elsewhere keeps the default of two retries unless the
    # provider turns them off.
    seen = Recorder(_status(500, {"error": {"message": "internal"}}))
    client = AsyncOpenAI(
        api_key="sk-test",
        base_url=BASE_URL,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(seen)),
        max_retries=2,
    )
    provider = OpenAICompatProvider(api_key="sk-test", client=client)

    with pytest.raises(ProviderError) as info:
        await _ask(provider)

    assert info.value.kind == ProviderErrorKind.SERVER
    assert len(seen.requests) == 1


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
