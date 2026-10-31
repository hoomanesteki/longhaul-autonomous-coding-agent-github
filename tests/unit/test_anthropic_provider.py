"""AnthropicProvider: request rendering, response parsing, streaming and error mapping.

The anthropic 2.x SDK sends requests through httpx2, so the mock transport comes from httpx2
and goes in through anthropic.DefaultAsyncHttpxClient. No network is used. The bodies follow
the shapes in the Claude API reference: a message, the message_start / content_block_* /
message_delta event stream, and the error envelope
{"type": "error", "error": {"type", "message"}, "request_id"}. ADR-0003 (appendix, parts 1
and 3) says where each shape was read.
"""

import email.utils
import json
import time
import types
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from longhaul.agent.render import to_anthropic
from longhaul.contracts import (
    CallOptions,
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    StopDetails,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from longhaul.providers.anthropic import AnthropicProvider
from longhaul.providers.base import get_provider
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase1]

MODEL = "claude-sonnet-5-5"
REQUEST_ID = "req_011CSHoEeqs5C35K2UUqR7Fy"
EPHEMERAL = {"type": "ephemeral"}
SAMPLING_PARAMETERS = {"temperature", "top_p", "top_k"}
# The kinds the contract calls retryable.
RETRYABLE = {
    ProviderErrorKind.RATE_LIMIT,
    ProviderErrorKind.OVERLOADED,
    ProviderErrorKind.SERVER,
    ProviderErrorKind.TIMEOUT,
}

Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture(autouse=True)
def _clean_anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The SDK reads these when it builds a client. A developer shell must not change a test.
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_LOG",
    ):
        monkeypatch.delenv(name, raising=False)


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


def _http_client(handler: Handler) -> anthropic.DefaultAsyncHttpxClient:
    return anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))


def _provider(handler: Handler) -> AnthropicProvider:
    return AnthropicProvider("sk-ant-test", http_client=_http_client(handler))


# ---------- documented bodies ----------
USAGE: dict[str, Any] = {
    "input_tokens": 12,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 6,
}
TEXT = {"type": "text", "text": "Let me check."}
# Under the default display setting a thinking block has an empty text and a signature.
THINKING = {"type": "thinking", "thinking": "", "signature": "EqQBCkYIBxgCKkDq0tiXk"}
REDACTED = {"type": "redacted_thinking", "data": "EmwKAhgBEgy3va3pzixsfE3gcigEi"}
TOOL_USE = {
    "type": "tool_use",
    "id": "toolu_01A09q90qw90lq917835lq9",
    "name": "read_file",
    "input": {"path": "src/calc/__init__.py"},
}
OTHER_TOOL_USE = {
    "type": "tool_use",
    "id": "toolu_01B1Nb2xx3Gi0sqm7QpUJ1Bf",
    "name": "run_tests",
    "input": {},
}


def _message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str | None = "end_turn",
    usage: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_01XFDUDYJgAACzvnptvVoYEL",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage or USAGE,
    }
    body.update(extra)
    return body


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}, "request_id": REQUEST_ID}


def _json(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> Handler:
    sent = {"request-id": REQUEST_ID, **(headers or {})}

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=body, headers=sent)

    return handler


def _raw(status: int, content: bytes, headers: dict[str, str] | None = None) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, content=content, headers=headers)

    return handler


def _ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=_message([TEXT]), headers={"request-id": REQUEST_ID})


def _timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("read timed out")


def _connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused")


# ---------- streamed bodies ----------
Events = list[tuple[str, dict[str, Any]]]
STREAM_HEADERS = {"content-type": "text/event-stream", "request-id": REQUEST_ID}


def _sse(events: Events) -> bytes:
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in events).encode()


def _block_events(index: int, block: dict[str, Any]) -> Events:
    """The events that build one content block, split the way the API splits them."""
    deltas: list[dict[str, Any]] = []
    start = block
    if block["type"] == "text":
        start = {"type": "text", "text": ""}
        deltas = [{"type": "text_delta", "text": block["text"]}]
    elif block["type"] == "tool_use":
        start = {**block, "input": {}}
        text = json.dumps(block["input"])
        half = len(text) // 2
        deltas = [
            {"type": "input_json_delta", "partial_json": p} for p in (text[:half], text[half:])
        ]
    elif block["type"] == "thinking":
        start = {"type": "thinking", "thinking": "", "signature": ""}
        deltas = [{"type": "signature_delta", "signature": block["signature"]}]
    events: Events = [
        (
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": start},
        )
    ]
    events += [
        ("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta})
        for delta in deltas
    ]
    events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    return events


def _events_for(body: dict[str, Any]) -> Events:
    """The event stream that carries the same reply as a plain message body."""
    usage = body["usage"]
    opening = {**body, "content": [], "stop_reason": None, "stop_details": None}
    opening["usage"] = {**usage, "output_tokens": 1}
    events: Events = [("message_start", {"type": "message_start", "message": opening})]
    for index, block in enumerate(body["content"]):
        events += _block_events(index, block)
    delta: dict[str, Any] = {"stop_reason": body["stop_reason"], "stop_sequence": None}
    if body.get("stop_details") is not None:
        delta["stop_details"] = body["stop_details"]
    closing = {
        "type": "message_delta",
        "delta": delta,
        "usage": {"output_tokens": usage["output_tokens"]},
    }
    return [*events, ("message_delta", closing), ("message_stop", {"type": "message_stop"})]


def _stream_response(events: Events) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=_sse(events), headers=STREAM_HEADERS)

    return handler


def _plain_or_stream(body: dict[str, Any]) -> Handler:
    """Answers a request with the plain body, or with the same reply as a stream."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if json.loads(request.content).get("stream"):
            return _stream_response(_events_for(body))(request)
        return _json(body)(request)

    return handler


class CutStream(httpx2.AsyncByteStream):
    """A response body that delivers some bytes and then fails, like a dropped connection."""

    def __init__(self, chunks: list[bytes], error: Exception) -> None:
        self._chunks = chunks
        self._error = error

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        raise self._error


# ---------- inputs ----------
READ_FILE = ToolSpec(
    name="read_file",
    description="Read a file",
    input_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)
RUN_TESTS = ToolSpec(
    name="run_tests", description="Run the tests", input_schema={"type": "object", "properties": {}}
)
FINISH = ToolSpec(
    name="finish",
    description="Finish the task",
    input_schema={
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
    strict=True,
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}
HI = [Message(role="user", content=[TextBlock(text="hi")])]
TRANSCRIPT = [
    Message(role="user", content=[TextBlock(text="fix it")]),
    Message(
        role="assistant",
        content=[
            TextBlock(text="Looking."),
            ToolUseBlock(id="toolu_01A", name="read_file", input={"path": "a.py"}),
            ToolUseBlock(id="toolu_01B", name="read_file", input={"path": "b.py"}),
        ],
    ),
    Message(
        role="user",
        content=[
            ToolResultBlock(tool_use_id="toolu_01A", content="print(1)"),
            ToolResultBlock(tool_use_id="toolu_01B", content="no such file", is_error=True),
            TextBlock(text="next"),
        ],
    ),
]


async def _ask(
    provider: AnthropicProvider,
    *,
    model: str = MODEL,
    system: str = "sys",
    messages: list[Message] | None = None,
    tools: list[ToolSpec] | None = None,
    max_tokens: int = 1024,
    cache_system: bool = True,
    options: CallOptions | None = None,
) -> LLMResponse:
    return await provider.complete(
        model=model,
        system=system,
        messages=messages or HI,
        tools=tools or [],
        max_tokens=max_tokens,
        cache_system=cache_system,
        options=options,
    )


# ---------- responses ----------
async def test_tool_use_parsed() -> None:
    usage = {
        "input_tokens": 120,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 64,
        "output_tokens": 45,
    }
    reply = _message(
        [TEXT, {**TOOL_USE, "input": {"path": "c.py"}}], stop_reason="tool_use", usage=usage
    )
    seen = Recorder(_json(reply))

    resp = await _provider(seen).complete(
        model=MODEL,
        system="You are a coder.",
        messages=TRANSCRIPT,
        tools=[READ_FILE],
        max_tokens=256,
    )

    request, body = seen.requests[0], seen.bodies[0]
    assert request.url.path == "/v1/messages"
    assert body["model"] == MODEL
    assert body["max_tokens"] == 256
    assert "stream" not in body
    assert body["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "fix it"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Looking."},
                {
                    "type": "tool_use",
                    "id": "toolu_01A",
                    "name": "read_file",
                    "input": {"path": "a.py"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_01B",
                    "name": "read_file",
                    "input": {"path": "b.py"},
                },
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01A",
                    "content": "print(1)",
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01B",
                    "content": "no such file",
                    "is_error": True,
                },
                {"type": "text", "text": "next"},
            ],
        },
    ]
    assert resp.stop_reason == "tool_use"
    assert resp.raw_stop_reason == "tool_use"
    assert resp.stop_details is None
    assert resp.content == [
        TextBlock(text="Let me check."),
        ToolUseBlock(id=TOOL_USE["id"], name="read_file", input={"path": "c.py"}),
    ]
    assert resp.tool_calls == [
        ToolCall(id=TOOL_USE["id"], name="read_file", input={"path": "c.py"})
    ]
    # The four buckets do not overlap: input_tokens is only the uncached remainder.
    assert resp.usage == Usage(
        input_tokens=120, output_tokens=45, cache_read_tokens=64, cache_write_tokens=30
    )
    assert resp.model == MODEL
    assert resp.request_id == REQUEST_ID
    assert resp.dropped_thinking_blocks == 0
    assert resp.latency_ms >= 0


async def test_tool_ids_and_inputs_come_back_exactly() -> None:
    odd_input = {
        "path": "src/ü.py",
        "ranges": [[1, 20], [40, 60]],
        "options": {"strict": True, "limit": None, "ratio": 0.5, "count": 3},
        "empty": {},
    }
    calls = [
        {**TOOL_USE, "id": "toolu_01", "input": odd_input},
        {**TOOL_USE, "id": "toolu_02", "input": {}},
        {**TOOL_USE, "id": "toolu_03", "name": "run_tests", "input": {"k": "v"}},
    ]
    resp = await _ask(_provider(_json(_message(calls, stop_reason="tool_use"))))

    assert [(c.id, c.name, c.input) for c in resp.tool_calls] == [
        ("toolu_01", "read_file", odd_input),
        ("toolu_02", "read_file", {}),
        ("toolu_03", "run_tests", {"k": "v"}),
    ]
    assert resp.content == [
        ToolUseBlock(id=c.id, name=c.name, input=c.input) for c in resp.tool_calls
    ]


async def test_text_only_reply() -> None:
    reply = _message([{"type": "text", "text": "All done."}])

    resp = await _ask(_provider(_json(reply)))

    assert resp.stop_reason == "end_turn"
    assert resp.content == [TextBlock(text="All done.")]
    assert resp.tool_calls == []


async def test_a_reply_with_no_content_is_returned_as_it_is() -> None:
    # The API sometimes ends a turn with no content. The loop decides what to do with that.
    resp = await _ask(_provider(_json(_message([]))))

    assert (resp.content, resp.tool_calls, resp.stop_reason) == ([], [], "end_turn")


@pytest.mark.parametrize(
    ("content", "stop_reason", "kept", "dropped"),
    [
        pytest.param(
            [THINKING, TEXT, TOOL_USE], "tool_use", [TEXT, TOOL_USE], 1, id="thinking-first"
        ),
        pytest.param(
            [TEXT, THINKING, TOOL_USE], "tool_use", [TEXT, TOOL_USE], 1, id="thinking-between"
        ),
        pytest.param(
            [TEXT, TOOL_USE, THINKING], "tool_use", [TEXT, TOOL_USE], 1, id="thinking-last"
        ),
        pytest.param([THINKING], "max_tokens", [], 1, id="thinking-only-max-tokens"),
        pytest.param(
            [THINKING, REDACTED, TEXT, THINKING, REDACTED],
            "end_turn",
            [TEXT],
            4,
            id="thinking-and-redacted",
        ),
        pytest.param([TEXT], "end_turn", [TEXT], 0, id="no-thinking"),
    ],
)
async def test_thinking_is_dropped_and_counted(
    content: list[dict[str, Any]],
    stop_reason: str,
    kept: list[dict[str, Any]],
    dropped: int,
) -> None:
    resp = await _ask(_provider(_json(_message(content, stop_reason=stop_reason))))

    # Blocks are read by type, so a thinking block in any position leaves the rest in order.
    expected = [
        TextBlock(text=b["text"])
        if b["type"] == "text"
        else ToolUseBlock(id=b["id"], name=b["name"], input=b["input"])
        for b in kept
    ]
    assert resp.content == expected
    assert [c.id for c in resp.tool_calls] == [b["id"] for b in kept if b["type"] == "tool_use"]
    assert resp.stop_reason == stop_reason
    assert resp.dropped_thinking_blocks == dropped


async def test_the_next_request_carries_no_thinking() -> None:
    # The stripped loop (D1): what goes back to the API after a thinking reply has none.
    replies = iter([_message([THINKING, TEXT, TOOL_USE], stop_reason="tool_use"), _message([TEXT])])

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=next(replies), headers={"request-id": REQUEST_ID})

    seen = Recorder(handler)
    provider = _provider(seen)
    first = await _ask(provider)
    transcript = [
        *HI,
        Message(role="assistant", content=first.content),
        Message(role="user", content=[ToolResultBlock(tool_use_id=TOOL_USE["id"], content="x")]),
    ]

    await _ask(provider, messages=transcript)

    sent = [block["type"] for m in seen.bodies[1]["messages"] for block in m["content"]]
    assert sent == ["text", "text", "tool_use", "tool_result"]


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(
            {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search", "input": {}},
            id="server_tool_use",
        ),
        pytest.param(
            {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_01", "content": []},
            id="web_search_tool_result",
        ),
        pytest.param({"type": "container_upload", "file_id": "file_01"}, id="container_upload"),
        pytest.param({"type": "mystery_block", "foo": "bar"}, id="not-known-to-the-sdk"),
    ],
)
async def test_unknown_block_type_raises_with_the_usage(block: dict[str, Any]) -> None:
    usage = {**USAGE, "input_tokens": 40, "output_tokens": 9, "cache_creation_input_tokens": 3}
    usage["cache_read_input_tokens"] = 7
    seen = Recorder(_json(_message([TEXT, block], usage=usage)))

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen))

    error = info.value
    assert error.kind is ProviderErrorKind.INVALID_REQUEST
    assert not error.retryable
    assert block["type"] in error.message
    # The call was billed, so the ledger needs the numbers (D5).
    assert error.usage == Usage(
        input_tokens=40, output_tokens=9, cache_read_tokens=7, cache_write_tokens=3
    )
    assert len(seen.requests) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("end_turn", "end_turn"),
        ("tool_use", "tool_use"),
        ("max_tokens", "max_tokens"),
        ("stop_sequence", "stop_sequence"),
        ("refusal", "refusal"),
        ("pause_turn", "pause_turn"),
        ("model_context_window_exceeded", "model_context_window_exceeded"),
        ("a_reason_added_next_year", "other"),
    ],
)
async def test_stop_reason_mapping(raw: str, expected: str) -> None:
    resp = await _ask(_provider(_json(_message([TEXT], stop_reason=raw))))

    assert resp.stop_reason == expected
    assert resp.raw_stop_reason == raw


async def test_refusal_reports_the_stop_details() -> None:
    details = {
        "type": "refusal",
        "category": "cyber",
        "explanation": "This request could enable cyber harm.",
    }
    reply = _message([], stop_reason="refusal", stop_details=details)

    resp = await _ask(_provider(_json(reply)))

    assert resp.stop_reason == "refusal"
    assert resp.raw_stop_reason == "refusal"
    assert resp.stop_details == StopDetails(
        category="cyber", explanation="This request could enable cyber harm."
    )
    assert resp.content == [] and resp.tool_calls == []


@pytest.mark.parametrize(
    ("details", "expected"),
    [
        pytest.param(None, None, id="null"),
        pytest.param(
            {"type": "refusal", "category": None, "explanation": None},
            StopDetails(),
            id="no-category-no-explanation",
        ),
        pytest.param(
            {"type": "refusal", "category": "bio", "explanation": None},
            StopDetails(category="bio"),
            id="no-explanation",
        ),
    ],
)
async def test_refusal_details_can_be_missing(
    details: dict[str, Any] | None, expected: StopDetails | None
) -> None:
    reply = _message([], stop_reason="refusal", stop_details=details)

    resp = await _ask(_provider(_json(reply)))

    assert resp.stop_reason == "refusal"
    assert resp.stop_details == expected


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param(
            {
                "input_tokens": 12,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 5,
                "cache_creation": {"ephemeral_5m_input_tokens": 3, "ephemeral_1h_input_tokens": 0},
                "output_tokens": 6,
                "output_tokens_details": {"thinking_tokens": 2},
                "service_tier": "standard",
            },
            Usage(input_tokens=12, output_tokens=6, cache_read_tokens=5, cache_write_tokens=3),
            id="all-fields",
        ),
        pytest.param(
            {
                "input_tokens": 12,
                "cache_creation_input_tokens": None,
                "cache_read_input_tokens": None,
                "output_tokens": 6,
            },
            Usage(input_tokens=12, output_tokens=6),
            id="null-cache-fields",
        ),
        pytest.param(
            {"input_tokens": 12, "output_tokens": 6},
            Usage(input_tokens=12, output_tokens=6),
            id="no-cache-fields",
        ),
    ],
)
async def test_usage_buckets(usage: dict[str, Any], expected: Usage) -> None:
    resp = await _ask(_provider(_json(_message([TEXT], usage=usage))))

    assert resp.usage == expected


async def test_request_id_comes_from_the_response_header() -> None:
    resp = await _ask(_provider(_json(_message([TEXT]), headers={"request-id": "req_abc123"})))

    assert resp.request_id == "req_abc123"


async def test_request_id_is_none_without_the_header() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=_message([TEXT]))

    assert (await _ask(_provider(handler))).request_id is None


async def test_the_response_carries_the_model_that_was_asked_for() -> None:
    resp = await _ask(_provider(_json(_message([TEXT]))), model="claude-haiku-5-5")

    assert resp.model == "claude-haiku-5-5"


@pytest.mark.parametrize("max_tokens", [1024, 64000])
async def test_latency_is_measured_around_the_call(
    monkeypatch: pytest.MonkeyPatch, max_tokens: int
) -> None:
    ticks = iter([100.0, 100.25])
    clock = types.SimpleNamespace(monotonic=lambda: next(ticks))
    monkeypatch.setattr("longhaul.providers.anthropic.time", clock)

    resp = await _ask(_provider(_plain_or_stream(_message([TEXT]))), max_tokens=max_tokens)

    assert resp.latency_ms == 250


# ---------- requests ----------
async def test_cache_control_placed() -> None:
    seen = Recorder(_ok)

    await _ask(
        _provider(seen),
        system="You are a coder.",
        messages=TRANSCRIPT,
        tools=[READ_FILE, RUN_TESTS, FINISH],
    )

    body = seen.bodies[0]
    assert body["system"] == [
        {"type": "text", "text": "You are a coder.", "cache_control": EPHEMERAL}
    ]
    assert [t["name"] for t in body["tools"]] == ["read_file", "run_tests", "finish"]
    assert ["cache_control" in t for t in body["tools"]] == [False, False, True]
    assert body["tools"][-1]["cache_control"] == EPHEMERAL
    # Two breakpoints in all: the system prompt and the last tool. The messages carry none.
    assert json.dumps(body).count("cache_control") == 2
    assert "cache_control" not in json.dumps(body["messages"])


async def test_cache_control_is_left_off_when_cache_system_is_false() -> None:
    seen = Recorder(_ok)

    await _ask(_provider(seen), tools=[READ_FILE, FINISH], cache_system=False)

    body = seen.bodies[0]
    assert body["system"] == [{"type": "text", "text": "sys"}]
    assert "cache_control" not in json.dumps(body)


async def test_tools_are_rendered_from_the_specs() -> None:
    seen = Recorder(_ok)

    await _ask(_provider(seen), tools=[READ_FILE, FINISH])

    first, last = seen.bodies[0]["tools"]
    assert first == {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": READ_FILE.input_schema,
    }
    # strict is sent only for a tool that asks for it.
    assert "strict" not in first
    assert last["strict"] is True
    assert last["input_schema"] == FINISH.input_schema


async def test_a_request_with_nothing_optional_leaves_those_fields_out() -> None:
    seen = Recorder(_ok)

    await _ask(_provider(seen), system="")

    assert set(seen.bodies[0]) == {"model", "max_tokens", "messages"}


async def test_a_blank_system_prompt_is_left_out() -> None:
    # A text block of only whitespace is a 400, and a 400 is not retried.
    seen = Recorder(_ok)

    await _ask(_provider(seen), system="  \n\t")

    assert "system" not in seen.bodies[0]


async def test_messages_are_rendered_by_to_anthropic() -> None:
    messages = [
        Message(role="user", content=[TextBlock(text="hi")]),
        Message(role="assistant", content=[TextBlock(text="")]),
        Message(role="user", content=[TextBlock(text="go on")]),
    ]
    seen = Recorder(_ok)

    await _ask(_provider(seen), messages=messages)

    assert seen.bodies[0]["messages"] == to_anthropic(messages)
    assert len(seen.bodies[0]["messages"]) == 2


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
async def test_effort_is_rendered(effort: str) -> None:
    seen = Recorder(_ok)

    await _ask(_provider(seen), options=CallOptions(effort=effort))

    assert seen.bodies[0]["output_config"] == {"effort": effort}


async def test_output_schema_is_rendered_as_a_structured_output() -> None:
    seen = Recorder(_ok)

    await _ask(_provider(seen), options=CallOptions(output_schema=SCHEMA))

    assert seen.bodies[0]["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}}


async def test_effort_and_output_schema_share_one_output_config() -> None:
    seen = Recorder(_ok)

    await _ask(_provider(seen), options=CallOptions(effort="low", output_schema=SCHEMA))

    assert seen.bodies[0]["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }


async def test_no_options_means_no_output_config_and_no_tool_choice() -> None:
    seen = Recorder(_ok)

    await _ask(_provider(seen), tools=[READ_FILE], options=CallOptions())

    assert "output_config" not in seen.bodies[0]
    assert "tool_choice" not in seen.bodies[0]


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5-5",
        "claude-opus-5-5",
        "claude-fable-5-1",
        "claude-mythos-5-1",
        # A dated snapshot of a model that rejects it rejects it too.
        "claude-sonnet-5-5-20261001",
    ],
)
async def test_force_tool_is_refused_before_any_http_call(model: str) -> None:
    seen = Recorder(_ok)

    with pytest.raises(ProviderError, match="force_tool") as info:
        await _ask(
            _provider(seen),
            model=model,
            tools=[READ_FILE, FINISH],
            options=CallOptions(force_tool="finish"),
        )

    assert info.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert not info.value.retryable
    assert model in info.value.message
    assert seen.requests == []


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-opus-5", "claude-sonnet-5"])
async def test_force_tool_becomes_a_tool_choice_where_the_model_takes_it(model: str) -> None:
    seen = Recorder(_ok)

    await _ask(
        _provider(seen),
        model=model,
        tools=[READ_FILE, FINISH],
        options=CallOptions(force_tool="finish"),
    )

    assert seen.bodies[0]["tool_choice"] == {"type": "tool", "name": "finish"}


def _by_path(body: dict[str, Any]) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/count_tokens"):
            return _json({"input_tokens": 77})(request)
        return _plain_or_stream(body)(request)

    return handler


async def test_no_sampling_parameters_are_sent() -> None:
    # On API version 2026-10-21 any of them is a 400 on every model (ADR-0003, D10).
    seen = Recorder(_by_path(_message([TEXT])))
    provider = _provider(seen)
    options = CallOptions(effort="high", force_tool="finish", output_schema=SCHEMA)
    kwargs: dict[str, Any] = {
        "model": "claude-haiku-5-5",
        "system": "sys",
        "messages": TRANSCRIPT,
        "tools": [READ_FILE, FINISH],
        "options": options,
    }

    await provider.complete(max_tokens=1024, **kwargs)
    await provider.complete(max_tokens=64000, **kwargs)
    await provider.complete(max_tokens=1024, cache_system=False, **kwargs)
    await provider.count_tokens(**kwargs)

    assert [r.url.path for r in seen.requests].count("/v1/messages") == 3
    assert len(seen.bodies) == 4
    for body in seen.bodies:
        assert not SAMPLING_PARAMETERS & set(body)


async def test_the_sdk_sends_the_api_version_adr_0003_records() -> None:
    # An SDK upgrade that moves the default version fails here on purpose, so a person
    # reads the new version's migration notes before the change merges.
    seen = Recorder(_ok)

    await _ask(_provider(seen))

    assert seen.requests[0].headers["anthropic-version"] == "2026-10-21"


# ---------- streaming ----------
@pytest.mark.parametrize(
    ("max_tokens", "streams"),
    [(1, False), (8192, False), (16000, False), (16001, True), (64000, True)],
)
async def test_streams_above_16000_tokens(max_tokens: int, streams: bool) -> None:
    seen = Recorder(_plain_or_stream(_message([TEXT])))

    await _ask(_provider(seen), max_tokens=max_tokens)

    body = seen.bodies[0]
    assert body["max_tokens"] == max_tokens
    assert body.get("stream", False) is streams
    assert len(seen.requests) == 1


STREAM_CASES = [
    pytest.param(
        _message(
            [THINKING, TEXT, TOOL_USE],
            stop_reason="tool_use",
            usage={**USAGE, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 64},
        ),
        id="thinking-text-tool",
    ),
    pytest.param(
        _message([TEXT, THINKING, TOOL_USE, REDACTED], stop_reason="tool_use"), id="mixed"
    ),
    pytest.param(_message([TEXT]), id="text"),
    pytest.param(_message([TOOL_USE, OTHER_TOOL_USE], stop_reason="tool_use"), id="two-tools"),
    pytest.param(_message([THINKING], stop_reason="max_tokens"), id="thinking-only-max-tokens"),
    pytest.param(
        _message(
            [],
            stop_reason="refusal",
            stop_details={"type": "refusal", "category": "bio", "explanation": "No."},
        ),
        id="refusal",
    ),
]


@pytest.mark.parametrize("body", STREAM_CASES)
async def test_a_streamed_reply_equals_the_plain_reply(body: dict[str, Any]) -> None:
    handler = _plain_or_stream(body)

    plain = await _ask(_provider(handler), max_tokens=16000)
    streamed = await _ask(_provider(handler), max_tokens=16001)

    assert streamed.model_dump(exclude={"latency_ms"}) == plain.model_dump(exclude={"latency_ms"})


async def test_a_streamed_reply_is_parsed() -> None:
    body = _message(
        [THINKING, TEXT, {**TOOL_USE, "input": {"path": "a/b.py", "lines": [1, 2]}}],
        stop_reason="tool_use",
        usage={**USAGE, "input_tokens": 25, "output_tokens": 15, "cache_read_input_tokens": 9},
    )
    seen = Recorder(_stream_response(_events_for(body)))

    resp = await _ask(_provider(seen), max_tokens=64000)

    assert resp.tool_calls == [
        ToolCall(id=TOOL_USE["id"], name="read_file", input={"path": "a/b.py", "lines": [1, 2]})
    ]
    assert resp.content[0] == TextBlock(text="Let me check.")
    assert resp.dropped_thinking_blocks == 1
    # message_start gives the input side, message_delta the final output count.
    assert resp.usage == Usage(input_tokens=25, output_tokens=15, cache_read_tokens=9)
    assert resp.request_id == REQUEST_ID
    assert resp.stop_reason == "tool_use"


async def test_a_stream_with_an_unknown_block_type_raises_with_the_usage() -> None:
    block = {"type": "server_tool_use", "id": "srvtoolu_01", "name": "web_search", "input": {}}
    body = _message([TEXT, block], usage={**USAGE, "input_tokens": 40, "output_tokens": 6})

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(_stream_response(_events_for(body))), max_tokens=64000)

    assert info.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert "server_tool_use" in info.value.message
    assert info.value.usage == Usage(input_tokens=40, output_tokens=6)


@pytest.mark.parametrize(
    ("error_type", "kind"),
    [
        ("overloaded_error", ProviderErrorKind.OVERLOADED),
        ("api_error", ProviderErrorKind.SERVER),
        ("rate_limit_error", ProviderErrorKind.RATE_LIMIT),
    ],
)
async def test_an_error_event_inside_a_stream_is_mapped(
    error_type: str, kind: ProviderErrorKind
) -> None:
    # The API answers 200 and reports the failure as an event, so the status says nothing.
    events = _events_for(_message([TEXT]))[:3]
    events.append(("error", _error(error_type, "Overloaded")))
    seen = Recorder(_stream_response(events))

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen), max_tokens=64000)

    assert info.value.kind is kind
    assert len(seen.requests) == 1


async def test_an_error_event_that_does_not_parse_is_a_server_error() -> None:
    chunk = _sse(_events_for(_message([TEXT]))[:1]) + b"event: error\ndata: upstream failure\n\n"
    seen = Recorder(_raw(200, chunk, STREAM_HEADERS))

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen), max_tokens=64000)

    assert info.value.kind is ProviderErrorKind.SERVER
    assert info.value.retryable


async def test_a_stream_cut_off_before_the_stop_reason_is_a_server_error() -> None:
    # No message_delta arrived, so the content may be half written. Retrying is right, and
    # the numbers from message_start still go to the ledger.
    events = _events_for(_message([TEXT]))[:-2]
    seen = Recorder(_stream_response(events))

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen), max_tokens=64000)

    assert info.value.kind is ProviderErrorKind.SERVER
    assert info.value.usage == Usage(input_tokens=12, output_tokens=1)
    assert len(seen.requests) == 1


async def test_an_empty_stream_is_a_server_error() -> None:
    with pytest.raises(ProviderError) as info:
        await _ask(_provider(_raw(200, b"", STREAM_HEADERS)), max_tokens=64000)

    assert info.value.kind is ProviderErrorKind.SERVER


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpx2.ReadTimeout("read timed out"), ProviderErrorKind.TIMEOUT),
        (httpx2.ReadError("connection reset"), ProviderErrorKind.SERVER),
        (httpx2.RemoteProtocolError("peer closed connection"), ProviderErrorKind.SERVER),
    ],
)
async def test_a_stream_that_fails_while_it_is_read_is_mapped(
    error: Exception, kind: ProviderErrorKind
) -> None:
    # The SDK wraps failures of the request, not of the body, so these arrive as httpx2 errors.
    chunk = _sse(_events_for(_message([TEXT]))[:2])

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=CutStream([chunk], error), headers=STREAM_HEADERS)

    seen = Recorder(handler)

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen), max_tokens=64000)

    assert info.value.kind is kind
    assert info.value.retryable
    assert len(seen.requests) == 1


# ---------- errors ----------
RATE_LIMITED = _error("rate_limit_error", "This request would exceed your rate limit.")
CONTEXT_MESSAGE = "prompt is too long: 241527 tokens > 200000 maximum"
ERROR_CASES = [
    pytest.param(
        _json(RATE_LIMITED, 429, {"retry-after": "7"}),
        ProviderErrorKind.RATE_LIMIT,
        7.0,
        id="429",
    ),
    pytest.param(
        _json(_error("overloaded_error", "Overloaded"), 529),
        ProviderErrorKind.OVERLOADED,
        None,
        id="529",
    ),
    pytest.param(
        _json(_error("api_error", "Internal server error"), 500),
        ProviderErrorKind.SERVER,
        None,
        id="500",
    ),
    pytest.param(_timeout, ProviderErrorKind.TIMEOUT, None, id="timeout"),
    pytest.param(
        _json(_error("invalid_request_error", CONTEXT_MESSAGE), 400),
        ProviderErrorKind.CONTEXT_TOO_LONG,
        None,
        id="context",
    ),
    pytest.param(
        _json(_error("authentication_error", "invalid x-api-key"), 401),
        ProviderErrorKind.AUTH,
        None,
        id="401",
    ),
    pytest.param(
        _json(_error("billing_error", "Your credit balance is too low."), 402),
        ProviderErrorKind.AUTH,
        None,
        id="402",
    ),
    pytest.param(
        _json(_error("permission_error", "Not allowed."), 403),
        ProviderErrorKind.AUTH,
        None,
        id="403",
    ),
    pytest.param(
        _json(_error("not_found_error", "model: claude-sonnet-9"), 404),
        ProviderErrorKind.INVALID_REQUEST,
        None,
        id="404",
    ),
    pytest.param(
        _json(_error("invalid_request_error", "messages: roles must alternate"), 400),
        ProviderErrorKind.INVALID_REQUEST,
        None,
        id="400",
    ),
    pytest.param(
        _json(_error("invalid_request_error", "unprocessable"), 422),
        ProviderErrorKind.INVALID_REQUEST,
        None,
        id="422",
    ),
    pytest.param(
        _json(_error("api_error", "Request timeout"), 408),
        ProviderErrorKind.TIMEOUT,
        None,
        id="408",
    ),
    pytest.param(
        _json(_error("api_error", "Conflict"), 409), ProviderErrorKind.SERVER, None, id="409"
    ),
    pytest.param(
        _json(_error("request_too_large", "Request exceeds the maximum size."), 413),
        ProviderErrorKind.CONTEXT_TOO_LONG,
        None,
        id="413",
    ),
    pytest.param(
        _json(_error("api_error", "Service unavailable"), 503),
        ProviderErrorKind.SERVER,
        None,
        id="503",
    ),
    pytest.param(
        _json(_error("timeout_error", "Gateway timeout"), 504),
        ProviderErrorKind.SERVER,
        None,
        id="504",
    ),
    pytest.param(
        _raw(502, b"<html><body>Bad gateway</body></html>", {"content-type": "text/html"}),
        ProviderErrorKind.SERVER,
        None,
        id="502-html",
    ),
    # Overload is told by the error type as well as by status 529.
    pytest.param(
        _json(_error("overloaded_error", "Overloaded"), 500),
        ProviderErrorKind.OVERLOADED,
        None,
        id="overloaded-type-on-500",
    ),
    pytest.param(_connect_error, ProviderErrorKind.SERVER, None, id="connect-error"),
]


@pytest.mark.parametrize(("handler", "kind", "retry_after_s"), ERROR_CASES)
async def test_error_mapping(
    handler: Handler, kind: ProviderErrorKind, retry_after_s: float | None
) -> None:
    seen = Recorder(handler)

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen))

    # One transport call per attempt. Temporal owns retries, and SDK retries would repeat paid
    # calls that the cost ledger never sees.
    assert len(seen.requests) == 1
    assert info.value.kind is kind
    assert info.value.retry_after_s == retry_after_s
    assert info.value.retryable == (kind in RETRYABLE)
    assert info.value.usage is None
    assert "sk-ant-test" not in info.value.message


def _cases(*ids: str) -> list[Any]:
    """The ERROR_CASES with these ids, for tests that need only some of them."""
    return [case for case in ERROR_CASES if case.id in ids]


@pytest.mark.parametrize(
    ("handler", "kind", "retry_after_s"),
    _cases("429", "529", "500", "timeout", "context", "401", "400", "502-html"),
)
async def test_error_mapping_when_streaming(
    handler: Handler, kind: ProviderErrorKind, retry_after_s: float | None
) -> None:
    seen = Recorder(handler)

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen), max_tokens=64000)

    assert len(seen.requests) == 1
    assert info.value.kind is kind
    assert info.value.retry_after_s == retry_after_s


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, ProviderErrorKind.INVALID_REQUEST),
        (401, ProviderErrorKind.AUTH),
        (402, ProviderErrorKind.AUTH),
        (403, ProviderErrorKind.AUTH),
        (404, ProviderErrorKind.INVALID_REQUEST),
        (408, ProviderErrorKind.TIMEOUT),
        (409, ProviderErrorKind.SERVER),
        (413, ProviderErrorKind.CONTEXT_TOO_LONG),
        (422, ProviderErrorKind.INVALID_REQUEST),
        (429, ProviderErrorKind.RATE_LIMIT),
        (500, ProviderErrorKind.SERVER),
        (502, ProviderErrorKind.SERVER),
        (503, ProviderErrorKind.SERVER),
        (504, ProviderErrorKind.SERVER),
        (529, ProviderErrorKind.OVERLOADED),
    ],
)
async def test_the_status_decides_when_the_body_says_nothing(
    status: int, kind: ProviderErrorKind
) -> None:
    # A proxy can answer with plain text. The table of statuses must hold without the error type.
    seen = Recorder(_raw(status, b"upstream said no", {"content-type": "text/plain"}))

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(seen))

    assert info.value.kind is kind
    assert len(seen.requests) == 1


async def test_a_prebuilt_client_is_held_to_zero_retries() -> None:
    seen = Recorder(_json(_error("api_error", "Internal server error"), 500))
    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test", max_retries=3, http_client=_http_client(seen)
    )

    with pytest.raises(ProviderError) as info:
        await _ask(AnthropicProvider("sk-ant-test", client=client))

    assert info.value.kind is ProviderErrorKind.SERVER
    assert len(seen.requests) == 1


def _with_retry_after(value: str) -> Handler:
    return _json(RATE_LIMITED, 429, {"retry-after": value})


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        pytest.param({"retry-after": "7"}, 7.0, id="seconds"),
        pytest.param({"retry-after": "2.5"}, 2.5, id="fractional-seconds"),
        pytest.param({"retry-after-ms": "1500"}, 1.5, id="milliseconds"),
        pytest.param({"retry-after-ms": "1500", "retry-after": "30"}, 1.5, id="ms-wins"),
        pytest.param({}, None, id="absent"),
        pytest.param({"retry-after": "0"}, None, id="zero"),
        pytest.param({"retry-after": "-3"}, None, id="negative"),
        pytest.param({"retry-after": "soon"}, None, id="words"),
        pytest.param({"retry-after": "nan"}, None, id="nan"),
        pytest.param({"retry-after": "inf"}, None, id="infinite"),
        pytest.param({"retry-after": str(10**9)}, None, id="more-than-a-day"),
        pytest.param({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, None, id="date-in-the-past"),
        pytest.param({"retry-after": "Wed, 21 Oct 9999 07:28:00 GMT"}, None, id="date-years-away"),
        pytest.param(
            {"retry-after": "Wed, 21 Oct 99999999999 07:28:00 GMT"}, None, id="date-out-of-range"
        ),
    ],
)
async def test_retry_after_is_parsed(headers: dict[str, str], expected: float | None) -> None:
    with pytest.raises(ProviderError) as info:
        await _ask(_provider(_json(RATE_LIMITED, 429, headers)))

    assert info.value.kind is ProviderErrorKind.RATE_LIMIT
    assert info.value.retry_after_s == expected


async def test_retry_after_may_be_an_http_date() -> None:
    date = email.utils.formatdate(time.time() + 30, usegmt=True)

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(_with_retry_after(date)))

    assert info.value.retry_after_s is not None
    assert 20 < info.value.retry_after_s <= 30


async def test_retry_after_is_kept_for_any_retryable_status() -> None:
    handler = _json(_error("overloaded_error", "Overloaded"), 529, {"retry-after": "12"})

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))

    assert info.value.kind is ProviderErrorKind.OVERLOADED
    assert info.value.retry_after_s == 12.0


async def test_a_header_on_a_failure_that_cannot_be_retried_is_ignored() -> None:
    handler = _json(_error("authentication_error", "invalid x-api-key"), 401, {"retry-after": "5"})

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))

    assert info.value.kind is ProviderErrorKind.AUTH
    assert info.value.retry_after_s is None


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(CONTEXT_MESSAGE, id="prompt-is-too-long"),
        pytest.param(
            "input length and `max_tokens` exceed context limit: 188240 + 21333 > 200000, "
            "decrease input length or `max_tokens` and try again",
            id="input-and-max-tokens",
        ),
        pytest.param("Input is too long for requested model.", id="gateway-wording"),
        pytest.param("PROMPT IS TOO LONG: 300000 tokens > 200000 maximum", id="upper-case"),
    ],
)
async def test_context_overflow_wordings(message: str) -> None:
    # The API gives no status or type for this, only the sentence (ADR-0003, section 2).
    handler = _json(_error("invalid_request_error", message), 400)

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))

    assert info.value.kind is ProviderErrorKind.CONTEXT_TOO_LONG
    assert not info.value.retryable


@pytest.mark.parametrize(
    "message",
    [
        pytest.param(
            'tool_choice: type "tool" and "any" are not supported for this model.',
            id="forced-tool-choice",
        ),
        pytest.param(
            '"thinking.type.disabled" is not supported for this model.', id="thinking-disabled"
        ),
        pytest.param(
            "messages.5.content.0: Invalid `signature` in `thinking` block. The block is bound "
            "to a different conversation.",
            id="thinking-signature",
        ),
        pytest.param("temperature: Extra inputs are not permitted", id="sampling-parameter"),
        pytest.param("max_tokens: 100000 > 64000, which is the maximum", id="max-tokens-limit"),
        pytest.param("Invalid value for 'context': expected an object", id="the-word-context"),
    ],
)
async def test_other_400s_stay_invalid_request(message: str) -> None:
    handler = _json(_error("invalid_request_error", message), 400)

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))

    assert info.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert not info.value.retryable


async def test_context_wording_on_a_5xx_stays_retryable() -> None:
    handler = _json(_error("api_error", CONTEXT_MESSAGE), 500)

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))

    assert info.value.kind is ProviderErrorKind.SERVER


async def test_the_error_message_carries_status_type_text_and_request_id() -> None:
    text = 'messages: roles must alternate between "user" and "assistant"'
    handler = _json(_error("invalid_request_error", text), 400)

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))

    message = info.value.message
    for part in ("400", "invalid_request_error", "roles must alternate", REQUEST_ID):
        assert part in message


async def test_the_request_id_falls_back_to_the_error_body() -> None:
    # A proxy can drop the header and leave the id in the body.
    body = json.dumps(_error("api_error", "Internal server error")).encode()

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(_raw(500, body, {"content-type": "application/json"})))

    assert REQUEST_ID in info.value.message


async def test_a_page_for_an_error_body_is_cut_short() -> None:
    page = b"<html>" + b"x" * 50_000 + b"</html>"

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(_raw(502, page, {"content-type": "text/html"})))

    assert info.value.kind is ProviderErrorKind.SERVER
    assert len(info.value.message) < 1000


async def test_a_success_status_with_a_page_for_a_body_is_a_server_error() -> None:
    handler = _raw(200, b"<html>hello</html>", {"content-type": "text/html"})

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))

    assert info.value.kind is ProviderErrorKind.SERVER


async def test_a_success_status_with_broken_json_is_a_server_error() -> None:
    handler = _raw(200, b"{not json", {"content-type": "application/json"})

    with pytest.raises(ProviderError) as info:
        await _ask(_provider(handler))

    assert info.value.kind is ProviderErrorKind.SERVER


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"unexpected": True}, id="not-a-message"),
        pytest.param({**_message([]), "content": None}, id="null-content"),
        pytest.param(_message([{"type": "text"}]), id="text-without-text"),
        pytest.param(_message([TEXT], stop_reason=None), id="no-stop-reason"),
    ],
)
async def test_a_body_that_is_not_a_message_is_a_server_error(body: dict[str, Any]) -> None:
    with pytest.raises(ProviderError) as info:
        await _ask(_provider(_json(body)))

    assert info.value.kind is ProviderErrorKind.SERVER
    assert info.value.retryable


# ---------- count_tokens ----------
async def test_count_tokens_returns_input_tokens() -> None:
    seen = Recorder(_json({"input_tokens": 2095}))

    count = await _provider(seen).count_tokens(
        model=MODEL,
        system="You are a coder.",
        messages=TRANSCRIPT,
        tools=[READ_FILE, FINISH],
        options=CallOptions(effort="low", output_schema=SCHEMA),
    )

    assert count == 2095
    assert len(seen.requests) == 1
    assert seen.requests[0].url.path == "/v1/messages/count_tokens"
    body = seen.bodies[0]
    # No create-only field goes out: max_tokens and stream are rejected by this endpoint.
    assert set(body) == {"model", "system", "messages", "tools", "output_config"}
    assert body["model"] == MODEL
    assert body["messages"] == to_anthropic(TRANSCRIPT)
    # A marker does not change a count, so none is sent.
    assert "cache_control" not in json.dumps(body)


async def test_count_tokens_sends_what_complete_sends() -> None:
    seen = Recorder(_by_path(_message([TEXT])))
    provider = _provider(seen)
    options = CallOptions(effort="medium", output_schema=SCHEMA)
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "system": "You are a coder.",
        "messages": TRANSCRIPT,
        "tools": [READ_FILE, RUN_TESTS, FINISH],
        "options": options,
    }

    await provider.complete(max_tokens=1024, **kwargs)
    await provider.count_tokens(**kwargs)

    sent, counted = seen.bodies

    def plain(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: v for k, v in t.items() if k != "cache_control"} for t in tools]

    for key in ("model", "messages", "output_config"):
        assert counted[key] == sent[key]
    assert counted["system"][0]["text"] == sent["system"][0]["text"]
    assert plain(counted["tools"]) == plain(sent["tools"])


async def test_count_tokens_carries_a_forced_tool_choice_where_the_model_takes_it() -> None:
    seen = Recorder(_json({"input_tokens": 5}))

    await _provider(seen).count_tokens(
        model="claude-haiku-5-5",
        system="sys",
        messages=HI,
        tools=[FINISH],
        options=CallOptions(force_tool="finish"),
    )

    assert seen.bodies[0]["tool_choice"] == {"type": "tool", "name": "finish"}


async def test_count_tokens_refuses_force_tool_before_any_http_call() -> None:
    seen = Recorder(_json({"input_tokens": 5}))

    with pytest.raises(ProviderError, match="force_tool") as info:
        await _provider(seen).count_tokens(
            model="claude-sonnet-5-5",
            system="sys",
            messages=HI,
            tools=[FINISH],
            options=CallOptions(force_tool="finish"),
        )

    assert info.value.kind is ProviderErrorKind.INVALID_REQUEST
    assert seen.requests == []


@pytest.mark.parametrize(
    ("handler", "kind", "retry_after_s"),
    _cases("429", "529", "500", "timeout", "context", "401", "413", "connect-error"),
)
async def test_count_tokens_maps_errors_the_same_way(
    handler: Handler, kind: ProviderErrorKind, retry_after_s: float | None
) -> None:
    seen = Recorder(handler)

    with pytest.raises(ProviderError) as info:
        await _provider(seen).count_tokens(model=MODEL, system="sys", messages=HI, tools=[])

    assert len(seen.requests) == 1
    assert info.value.kind is kind
    assert info.value.retry_after_s == retry_after_s


async def test_count_tokens_with_a_body_that_is_not_a_count_is_a_server_error() -> None:
    with pytest.raises(ProviderError) as info:
        await _provider(_json({"unexpected": True})).count_tokens(
            model=MODEL, system="sys", messages=HI, tools=[]
        )

    assert info.value.kind is ProviderErrorKind.SERVER


# ---------- construction and the factory ----------
def test_the_provider_is_named_anthropic() -> None:
    assert AnthropicProvider("sk-ant-test").name == "anthropic"


def test_get_provider_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        get_provider(EnvSettings(provider="anthropic", _env_file=None))

    settings = EnvSettings(
        provider="anthropic", ANTHROPIC_API_KEY=SecretStr("sk-ant-test"), _env_file=None
    )
    provider = get_provider(settings)

    assert isinstance(provider, AnthropicProvider)
    assert provider.name == "anthropic"


def test_get_provider_hands_the_key_over_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    class Recording:
        name = "anthropic"

        def __init__(self, api_key: str) -> None:
            seen.append(api_key)

    monkeypatch.setattr("longhaul.providers.anthropic.AnthropicProvider", Recording)
    settings = EnvSettings(
        provider="anthropic", ANTHROPIC_API_KEY=SecretStr("sk-ant-test"), _env_file=None
    )

    get_provider(settings)

    assert seen == ["sk-ant-test"]
