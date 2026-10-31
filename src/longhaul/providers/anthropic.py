"""Provider for the Anthropic Messages API.

The anthropic SDK does the HTTP. This module renders contracts into a request, parses the
reply into an LLMResponse, and maps every failure to a ProviderError. SDK retries are off
(max_retries=0) because the durable layer owns them (LH-204): an SDK retry would repeat a paid
call that the activity retry policy and the cost ledger never see. That makes the error
mapping the only place that knows what is transient, so it carries over what the SDK would
have retried: connection errors, 408, 409, 429 and 5xx.

Whatever the API sends back, complete() returns an LLMResponse or raises ProviderError. LH-204
retries by kind and treats any other exception as a bug.

Requests. The system prompt goes out as one text block. With cache_system set it carries a
cache_control breakpoint, and so does the last tool, which caches tools and system together.
The messages get no breakpoint here; LH-405 adds that. ToolSpec.strict is sent when true.
CallOptions.effort and output_schema share one output_config. A forced tool choice is refused
before any HTTP call for the models that answer it with a 400 (ADR-0003, D3). No temperature,
top_p or top_k is ever sent: API version 2026-10-21 rejects all three (D10).

A reply is streamed when max_tokens is above 16000 and sent plain otherwise. The SDK refuses a
plain call that may run past ten minutes, and the retry after a max_tokens stop asks for 64000
(D8).

Replies. Content is read by block type, never by position. text and tool_use blocks become
contract blocks as returned. thinking and redacted_thinking blocks are dropped and counted in
dropped_thinking_blocks (D1). Any other block type raises INVALID_REQUEST with the usage, so
the cost ledger still sees a billed call. stop_reason maps to the contract values with
anything new as "other", and raw_stop_reason keeps the API's own string.

Usage buckets do not overlap: input_tokens is only the uncached remainder, as the API reports
it. Cost (LH-701) and cache_ratio (LH-405) add the buckets up.
"""

import email.utils
import json
import math
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, NamedTuple, cast

import anthropic
import httpx2
from anthropic import Omit, omit
from anthropic.types import (
    CacheControlEphemeralParam,
    MessageParam,
    OutputConfigParam,
    TextBlockParam,
    ToolChoiceToolParam,
    ToolParam,
)
from anthropic.types import Message as ApiMessage

from longhaul.agent.render import to_anthropic
from longhaul.contracts import (
    Block,
    CallOptions,
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    StopDetails,
    StopReason,
    TextBlock,
    ToolCall,
    ToolSpec,
    ToolUseBlock,
    Usage,
)

# Above this the reply is streamed. The SDK raises ValueError before sending a plain request
# that it expects to run past ten minutes (about 21,000 tokens at the default timeout).
_STREAM_ABOVE_TOKENS = 16_000
# Models that answer tool_choice "tool" or "any" with a 400 (python/claude-api/tool-use.md,
# "Tool Choice"). A dated snapshot of one of them rejects it too.
_NO_FORCED_TOOL_CHOICE = (
    "claude-sonnet-5-5",
    "claude-opus-5-5",
    "claude-fable-5-1",
    "claude-mythos-5-1",
)

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})
_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "stop_sequence": "stop_sequence",
    "refusal": "refusal",
    "pause_turn": "pause_turn",
    "model_context_window_exceeded": "model_context_window_exceeded",
}

# The status decides first. The SDK has no class for 402 or 408, and the docs name none for
# 409 or 413, so the status is the one thing every case has.
_STATUS_KINDS: dict[int, ProviderErrorKind] = {
    401: ProviderErrorKind.AUTH,
    402: ProviderErrorKind.AUTH,
    403: ProviderErrorKind.AUTH,
    408: ProviderErrorKind.TIMEOUT,
    409: ProviderErrorKind.SERVER,
    413: ProviderErrorKind.CONTEXT_TOO_LONG,
    429: ProviderErrorKind.RATE_LIMIT,
    529: ProviderErrorKind.OVERLOADED,
}
# The error type matters when the status does not say enough: an error event inside a stream
# arrives with status 200, and overload also comes as a 5xx other than 529.
_TYPE_KINDS: dict[str, ProviderErrorKind] = {
    "rate_limit_error": ProviderErrorKind.RATE_LIMIT,
    "overloaded_error": ProviderErrorKind.OVERLOADED,
    "api_error": ProviderErrorKind.SERVER,
    "authentication_error": ProviderErrorKind.AUTH,
    "permission_error": ProviderErrorKind.AUTH,
    "billing_error": ProviderErrorKind.AUTH,
    "request_too_large": ProviderErrorKind.CONTEXT_TOO_LONG,
}
# The API gives no status or type for a prompt over the window, only a sentence in a 400
# (ADR-0003, appendix section 2). Lower case, matched against the API's message alone.
_CONTEXT_MARKERS = (
    "prompt is too long",
    "input is too long",
    "exceed context limit",
    "exceeds the context window",
    "too many tokens",
)
# Longest retry-after the provider passes on. A longer one is more likely a fault than a real
# wait, and a huge float would overflow timedelta in the retry policy.
_MAX_RETRY_AFTER_S = 24 * 60 * 60
# An error page from a gateway can be large. The message stays short enough for a log line.
_MESSAGE_CHARS = 500


class _Reply(NamedTuple):
    message: object
    request_id: str | None


@dataclass(frozen=True)
class _Request:
    """The parts of a request that complete() and count_tokens() share."""

    model: str
    messages: list[MessageParam]
    system: list[TextBlockParam] | Omit
    tools: list[ToolParam] | Omit
    tool_choice: ToolChoiceToolParam | Omit
    output_config: OutputConfigParam | Omit


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        # A prebuilt client wins. Tests pass an http_client backed by MockTransport.
        if client is None:
            client = anthropic.AsyncAnthropic(
                api_key=api_key, max_retries=0, http_client=http_client
            )
        elif client.max_retries != 0:
            # The durable layer owns retries, so a client built elsewhere is held to the same rule.
            client = client.with_options(max_retries=0)
        self._client = client

    async def complete(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        cache_system: bool = True,
        options: CallOptions | None = None,
    ) -> LLMResponse:
        request = _build_request(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            cache_system=cache_system,
            options=options,
        )
        started = time.monotonic()
        if max_tokens > _STREAM_ABOVE_TOKENS:
            reply = await _guarded(self._stream(request, max_tokens))
        else:
            reply = await _guarded(self._create(request, max_tokens))
        latency_ms = int((time.monotonic() - started) * 1000)
        return _to_response(
            reply.message, model=model, request_id=reply.request_id, latency_ms=latency_ms
        )

    async def count_tokens(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        options: CallOptions | None = None,
    ) -> int:
        # Same system, tools and options as complete(), so the count is for the prompt that
        # complete() would send. Markers do not change a count, so none are rendered.
        request = _build_request(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            cache_system=False,
            options=options,
        )
        counted = await _guarded(
            self._client.messages.count_tokens(
                model=request.model,
                messages=request.messages,
                system=request.system,
                tools=request.tools,
                tool_choice=request.tool_choice,
                output_config=request.output_config,
            )
        )
        tokens = getattr(counted, "input_tokens", None)
        if not isinstance(tokens, int) or isinstance(tokens, bool):
            raise ProviderError(
                ProviderErrorKind.SERVER, f"response was not a token count: {_snippet(counted)}"
            )
        return tokens

    async def _create(self, request: _Request, max_tokens: int) -> _Reply:
        message = await self._client.messages.create(
            model=request.model,
            max_tokens=max_tokens,
            messages=request.messages,
            system=request.system,
            tools=request.tools,
            tool_choice=request.tool_choice,
            output_config=request.output_config,
        )
        # The SDK copies the request-id header onto a plain reply. A stream has the header on
        # the stream object instead, below.
        return _Reply(message, _clean_id(getattr(message, "_request_id", None)))

    async def _stream(self, request: _Request, max_tokens: int) -> _Reply:
        async with self._client.messages.stream(
            model=request.model,
            max_tokens=max_tokens,
            messages=request.messages,
            system=request.system,
            tools=request.tools,
            tool_choice=request.tool_choice,
            output_config=request.output_config,
        ) as stream:
            try:
                message = await stream.get_final_message()
            except (AssertionError, RuntimeError, ValueError) as exc:
                # The SDK asserts that message_start arrived, raises RuntimeError for events
                # out of order and ValueError for tool input it cannot parse. All of them
                # mean a stream that went wrong, which a retry may clear.
                raise ProviderError(
                    ProviderErrorKind.SERVER, f"could not read the response stream: {exc!r}"
                ) from exc
            return _Reply(message, _clean_id(stream.request_id))


# ---------- contracts -> request ----------
def _build_request(
    *,
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    cache_system: bool,
    options: CallOptions | None,
) -> _Request:
    # The forced tool check comes first, so a refused request makes no HTTP call.
    tool_choice = _tool_choice(model, options)
    return _Request(
        model=model,
        messages=cast(list[MessageParam], to_anthropic(messages)),
        system=_system_param(system, cache_system),
        tools=_tool_params(tools, cache_system),
        tool_choice=tool_choice,
        output_config=_output_config(options),
    )


def _cache_control() -> CacheControlEphemeralParam:
    return {"type": "ephemeral"}


def _system_param(system: str, cache: bool) -> list[TextBlockParam] | Omit:
    # A text block of only whitespace is a 400, and a 400 is not retried.
    if not system.strip():
        return omit
    block: TextBlockParam = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = _cache_control()
    return [block]


def _tool_params(tools: list[ToolSpec], cache: bool) -> list[ToolParam] | Omit:
    if not tools:
        return omit
    params = [_tool_param(spec) for spec in tools]
    if cache:
        # Tools render before the system prompt, so a marker on the last tool caches them all.
        params[-1]["cache_control"] = _cache_control()
    return params


def _tool_param(spec: ToolSpec) -> ToolParam:
    param: ToolParam = {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    }
    if spec.strict:
        param["strict"] = True
    return param


def _tool_choice(model: str, options: CallOptions | None) -> ToolChoiceToolParam | Omit:
    if options is None or options.force_tool is None:
        return omit
    if _rejects_forced_tool_choice(model):
        raise ProviderError(
            ProviderErrorKind.INVALID_REQUEST,
            f"force_tool={options.force_tool!r} is not supported for model {model!r}: the API "
            "answers a forced tool_choice with a 400. Use output_schema, or a strict tool with "
            "a prompt that names it.",
        )
    return {"type": "tool", "name": options.force_tool}


def _rejects_forced_tool_choice(model: str) -> bool:
    return any(model == known or model.startswith(f"{known}-") for known in _NO_FORCED_TOOL_CHOICE)


def _output_config(options: CallOptions | None) -> OutputConfigParam | Omit:
    if options is None:
        return omit
    config: OutputConfigParam = {}
    if options.effort is not None:
        config["effort"] = options.effort
    if options.output_schema is not None:
        config["format"] = {"type": "json_schema", "schema": options.output_schema}
    return config or omit


# ---------- reply -> contracts ----------
def _to_response(
    message: object, *, model: str, request_id: str | None, latency_ms: int
) -> LLMResponse:
    if not isinstance(message, ApiMessage):
        # A gateway answered 200 with a page. The SDK hands back the raw text in that case.
        raise ProviderError(
            ProviderErrorKind.SERVER, f"response was not a message: {_snippet(message)}"
        )
    usage = _usage(getattr(message, "usage", None))
    try:
        return _convert(
            message, usage=usage, model=model, request_id=request_id, latency_ms=latency_ms
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # The SDK builds models without checking them, so a field that a server left out or
        # mistyped fails here, not in the SDK. The call was billed, so the usage goes along.
        raise ProviderError(
            ProviderErrorKind.SERVER, f"unexpected response shape: {exc!r}", usage=usage
        ) from exc


def _convert(
    message: ApiMessage, *, usage: Usage, model: str, request_id: str | None, latency_ms: int
) -> LLMResponse:
    content: list[Block] = []
    tool_calls: list[ToolCall] = []
    dropped = 0
    for raw in message.content:
        # Read by type name. The SDK builds a block type it does not know as a TextBlock that
        # keeps the unknown name, so isinstance would take it for text.
        block: Any = raw
        kind = _block_type(block)
        if kind == "text":
            content.append(TextBlock(text=block.text))
        elif kind == "tool_use":
            call = ToolCall(id=block.id, name=block.name, input=block.input)
            tool_calls.append(call)
            content.append(ToolUseBlock(id=call.id, name=call.name, input=call.input))
        elif kind in _THINKING_TYPES:
            dropped += 1
        else:
            raise ProviderError(
                ProviderErrorKind.INVALID_REQUEST,
                f"the response has a content block of type {kind!r}, which this provider does "
                "not read (it keeps text and tool_use and drops thinking)",
                usage=usage,
            )
    stop_reason = message.stop_reason
    if stop_reason is None:
        # A plain reply always has one. A stream that ends without it was cut off, and its
        # content may be half written.
        raise ProviderError(
            ProviderErrorKind.SERVER,
            "the response ended without a stop_reason, so the stream was cut off",
            usage=usage,
        )
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        stop_reason=_STOP_REASONS.get(stop_reason, "other"),
        raw_stop_reason=stop_reason if isinstance(stop_reason, str) else None,
        stop_details=_stop_details(message.stop_details),
        usage=usage,
        model=model,
        latency_ms=latency_ms,
        request_id=request_id,
        dropped_thinking_blocks=dropped,
    )


def _block_type(block: object) -> str:
    kind = getattr(block, "type", None)
    return kind if isinstance(kind, str) else "<missing>"


def _stop_details(raw: object) -> StopDetails | None:
    if raw is None:
        return None
    return StopDetails(
        category=_text_or_none(getattr(raw, "category", None)),
        explanation=_text_or_none(getattr(raw, "explanation", None)),
    )


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _clean_id(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _usage(raw: object) -> Usage:
    # The cache fields are null when no cache was involved. A counter that is missing or
    # mistyped is zero, so a billed call still comes back usable.
    return Usage(
        input_tokens=_count(getattr(raw, "input_tokens", None)),
        output_tokens=_count(getattr(raw, "output_tokens", None)),
        cache_read_tokens=_count(getattr(raw, "cache_read_input_tokens", None)),
        cache_write_tokens=_count(getattr(raw, "cache_creation_input_tokens", None)),
    )


def _count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


def _snippet(body: object) -> str:
    text = body if isinstance(body, str) else repr(body)
    return f"{type(body).__name__}: {text[:200]}"


# ---------- failures ----------
async def _guarded[T](call: Awaitable[T]) -> T:
    """Await an SDK call and turn every failure it can raise into a ProviderError."""
    try:
        return await call
    except anthropic.APIError as exc:
        raise _map_api_error(exc) from exc
    except httpx2.TimeoutException as exc:
        # The SDK wraps failures of the request, not of a stream body being read.
        raise ProviderError(ProviderErrorKind.TIMEOUT, _transport_text(exc)) from exc
    except httpx2.HTTPError as exc:
        raise ProviderError(ProviderErrorKind.SERVER, _transport_text(exc)) from exc
    except json.JSONDecodeError as exc:
        # A 200 that claims to be JSON and is not, from a gateway or a broken server.
        raise ProviderError(
            ProviderErrorKind.SERVER, f"anthropic: response body was not valid JSON: {exc}"
        ) from exc


def _transport_text(exc: Exception) -> str:
    return f"anthropic: {type(exc).__name__}: {exc}"


def _map_api_error(exc: anthropic.APIError) -> ProviderError:
    # Most specific class first. APITimeoutError is a kind of APIConnectionError, which is
    # not an APIStatusError.
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError(ProviderErrorKind.TIMEOUT, f"anthropic: {exc.message}")
    if not isinstance(exc, anthropic.APIStatusError):
        # Connection failures, and bodies the SDK could not read.
        return ProviderError(ProviderErrorKind.SERVER, f"anthropic: {exc.message}")
    kind = _status_kind(exc.status_code, exc.type, _api_message(exc))
    error = ProviderError(kind, _describe(exc))
    if error.retryable:
        # Any transient status can carry the hint, a 529 as well as a 429.
        error.retry_after_s = _retry_after(exc.response.headers)
    return error


def _status_kind(status: int, error_type: str | None, text: str) -> ProviderErrorKind:
    by_status = _STATUS_KINDS.get(status)
    if by_status is not None:
        return by_status
    by_type = _TYPE_KINDS.get(error_type or "")
    if status >= 500:
        if by_type is ProviderErrorKind.OVERLOADED:
            return by_type
        return ProviderErrorKind.SERVER
    if by_type is not None:
        return by_type
    if status < 400:
        # An error event inside a stream whose type we cannot read. The server failed, not
        # the request.
        return ProviderErrorKind.SERVER
    # Checked for client errors only. A 5xx stays retryable whatever its text says.
    if any(marker in text.lower() for marker in _CONTEXT_MARKERS):
        return ProviderErrorKind.CONTEXT_TOO_LONG
    return ProviderErrorKind.INVALID_REQUEST


def _api_message(exc: anthropic.APIStatusError) -> str:
    # The API's own sentence is body["error"]["message"]. exc.message holds the whole body
    # as text, or the raw text of a body that is not JSON.
    body = exc.body
    error = body.get("error") if isinstance(body, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    return message if isinstance(message, str) else exc.message


def _describe(exc: anthropic.APIStatusError) -> str:
    detail = _api_message(exc)
    if len(detail) > _MESSAGE_CHARS:
        detail = detail[:_MESSAGE_CHARS] + "..."
    error_type = f" {exc.type}" if exc.type else ""
    # The header is the usual source. A proxy may drop it and leave the copy in the body.
    body_id = exc.body.get("request_id") if isinstance(exc.body, dict) else None
    request_id = _clean_id(exc.request_id) or _clean_id(body_id)
    suffix = f" (request_id {request_id})" if request_id else ""
    return f"anthropic {exc.status_code}{error_type}: {detail}{suffix}"


def _retry_after(headers: httpx2.Headers) -> float | None:
    # Same order as the SDK: milliseconds, seconds, then an HTTP date. A form that gives
    # nothing usable falls through to the next one. None sends the durable layer back to its
    # own backoff.
    seconds = headers.get("retry-after")
    for delay in (
        _parse_seconds(headers.get("retry-after-ms"), divisor=1000),
        _parse_seconds(seconds),
        _parse_http_date(seconds),
    ):
        if delay is not None and math.isfinite(delay) and 0 < delay <= _MAX_RETRY_AFTER_S:
            return delay
    return None


def _parse_seconds(raw: str | None, divisor: float = 1) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw) / divisor
    except ValueError:
        return None


def _parse_http_date(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        parsed = email.utils.parsedate_tz(raw)
        return None if parsed is None else email.utils.mktime_tz(parsed) - time.time()
    except (TypeError, ValueError, OverflowError, OSError):
        return None
