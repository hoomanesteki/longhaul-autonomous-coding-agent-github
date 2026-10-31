"""Provider for any endpoint that speaks the OpenAI chat completions format.

The openai SDK does the HTTP. This module converts between contracts and the
chat completions shape, and maps SDK errors to ProviderErrorKind. Retries are
off (max_retries=0) because the durable layer owns them. That makes the error
mapping the only place that knows what is transient, so it carries over what the
SDK would have retried: 408, 409, 429, 5xx and the x-should-retry header.

Whatever a server sends back, complete() returns a response or raises
ProviderError. LH-204 retries by kind and treats any other exception as a bug.

cache_system is accepted for protocol compatibility and ignored. Chat
completions has no cache_control marker; caching, when the endpoint does it,
happens on the server side.

Call options. effort is ignored: chat completions has no setting that means the
same. force_tool becomes a function tool_choice, and it must name one of the
offered tools, or the call fails with INVALID_REQUEST before any request is sent.
output_schema becomes a strict json_schema response_format. ToolSpec.strict is not
sent. Stop reasons are unchanged: a refusal is still reported as "other", with the
refusal text in the content. raw_stop_reason is the finish_reason the server sent,
and request_id is the x-request-id header when there is one.

The reply cap is sent as max_completion_tokens to OpenAI's own endpoint and as
max_tokens to every other server. Pass max_tokens_param to override the choice.

Usage buckets do not overlap, as with the Anthropic provider: input_tokens
leaves out cache_read_tokens. Chat completions reports prompt_tokens with the
cached tokens inside it, so the cached part is taken out here. Cost (LH-701)
and cache_ratio (LH-405) add the buckets up and would count cached tokens twice
otherwise.

Tool arguments that are not a JSON object come back as {"_raw": text}, for the
tool registry to report. An empty argument string means no arguments: {}.
"""

import email.utils
import json
import math
import time
from typing import Any, Literal

import httpx2
import openai
from openai import AsyncOpenAI, Omit, omit
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageFunctionToolCall,
    ChatCompletionMessageFunctionToolCallParam,
    ChatCompletionMessageParam,
    ChatCompletionToolChoiceOptionParam,
    ChatCompletionToolUnionParam,
)
from openai.types.completion_usage import CompletionUsage
from openai.types.shared_params import ResponseFormatJSONSchema

from longhaul.contracts import (
    Block,
    CallOptions,
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

_StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "other"]

# Name of the request field that caps the reply. OpenAI's reasoning models reject
# the deprecated max_tokens; most other servers know only that name.
MaxTokensParam = Literal["max_tokens", "max_completion_tokens"]
_MAX_TOKENS_PARAMS: tuple[MaxTokensParam, ...] = ("max_tokens", "max_completion_tokens")
_OPENAI_HOST = "api.openai.com"
# response_format needs a name for the schema. Callers pass a schema, not a name.
_SCHEMA_NAME = "output"

# Keys are the chat completions finish_reason values.
_STOP_REASONS: dict[str, _StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}
# Lowercase fragments of the codes, types and messages that servers use when the
# input does not fit. OpenAI sends the code. llama.cpp sends the type. vLLM,
# TGI, Gemini and others only write a sentence, in their own words.
_CONTEXT_MARKERS = (
    "context_length_exceeded",
    "exceed_context_size_error",
    "context length",
    "context window",
    "context size",
    "exceeds the maximum number of tokens",
    "`inputs` tokens + `max_new_tokens`",
    "prompt is too long",
    "input is too long",
)
_REQUEST_TOO_LARGE = "request too large"
# Longest Retry-After the provider passes on. A longer one is more likely a fault
# than a real wait, and a huge float would overflow timedelta in the retry policy.
_MAX_RETRY_AFTER_S = 24 * 60 * 60
_OVERLOADED_STATUS = 529
_OVERLOADED_503_TEXT = "overloaded"
_SNIPPET_CHARS = 200
_CHARS_PER_TOKEN = 4
# The kinds the contract calls retryable, read from it so the two cannot drift.
_TRANSIENT_KINDS = frozenset(k for k in ProviderErrorKind if ProviderError(k, "").retryable)


class OpenAICompatProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
        http_client: httpx2.AsyncClient | None = None,
        max_tokens_param: MaxTokensParam | None = None,
    ) -> None:
        if max_tokens_param is not None and max_tokens_param not in _MAX_TOKENS_PARAMS:
            raise ValueError(
                f"max_tokens_param must be one of {_MAX_TOKENS_PARAMS}, got {max_tokens_param!r}"
            )
        # A prebuilt client wins. Tests pass an http_client backed by MockTransport.
        if client is None:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                http_client=http_client,
            )
        elif client.max_retries != 0:
            # The durable layer owns retries. SDK retries would repeat paid calls
            # that the activity retry policy and the cost ledger never see.
            client = client.with_options(max_retries=0)
        self._client = client
        self._max_tokens_param = max_tokens_param or _default_max_tokens_param(client)

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
        chat_tools: list[ChatCompletionToolUnionParam] | Omit = (
            [_tool_param(t) for t in tools] if tools else omit
        )
        chat_messages = _chat_messages(system, messages)
        tool_choice = _tool_choice(options, tools)
        response_format = _response_format(options)
        started = time.monotonic()
        try:
            completion = await self._client.chat.completions.create(
                model=model,
                messages=chat_messages,
                tools=chat_tools,
                tool_choice=tool_choice,
                response_format=response_format,
                max_tokens=max_tokens if self._max_tokens_param == "max_tokens" else omit,
                max_completion_tokens=(
                    max_tokens if self._max_tokens_param == "max_completion_tokens" else omit
                ),
            )
        except openai.APIError as exc:
            raise _map_error(exc) from exc
        except json.JSONDecodeError as exc:
            # A 200 that claims to be JSON and is not, from a gateway or a broken server.
            raise ProviderError(
                ProviderErrorKind.SERVER, f"response body was not valid JSON: {exc}"
            ) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        return _to_response(completion, model=model, latency_ms=latency_ms)

    async def count_tokens(
        self,
        *,
        model: str,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        options: CallOptions | None = None,
    ) -> int:
        return _estimate(system, messages)


def _default_max_tokens_param(client: AsyncOpenAI) -> MaxTokensParam:
    # Judge by where the client really points, so a prebuilt client or a base URL
    # taken from the environment gets the right name too. OpenAI also serves
    # regional hosts such as eu.api.openai.com.
    host = client.base_url.host
    official = host == _OPENAI_HOST or host.endswith(f".{_OPENAI_HOST}")
    return "max_completion_tokens" if official else "max_tokens"


# ---------- contracts -> chat completions ----------
def _tool_param(spec: ToolSpec) -> ChatCompletionToolUnionParam:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.input_schema,
        },
    }


def _tool_choice(
    options: CallOptions | None, tools: list[ToolSpec]
) -> ChatCompletionToolChoiceOptionParam | Omit:
    if options is None or options.force_tool is None:
        return omit
    offered = [t.name for t in tools]
    if options.force_tool not in offered:
        # The server would answer 400. Saying so here saves the call and names the option.
        raise ProviderError(
            ProviderErrorKind.INVALID_REQUEST,
            f"force_tool {options.force_tool!r} is not one of the offered tools: {offered}",
        )
    return {"type": "function", "function": {"name": options.force_tool}}


def _response_format(options: CallOptions | None) -> ResponseFormatJSONSchema | Omit:
    if options is None or options.output_schema is None:
        return omit
    # strict, because the point of an output schema is a reply that always parses.
    return {
        "type": "json_schema",
        "json_schema": {"name": _SCHEMA_NAME, "schema": options.output_schema, "strict": True},
    }


def _chat_messages(system: str, messages: list[Message]) -> list[ChatCompletionMessageParam]:
    out: list[ChatCompletionMessageParam] = [{"role": "system", "content": system}]
    for message in messages:
        if message.role == "assistant":
            out.append(_assistant_message(message))
        else:
            out.extend(_user_messages(message))
    return out


def _assistant_message(message: Message) -> ChatCompletionMessageParam:
    text = "\n".join(b.text for b in message.content if isinstance(b, TextBlock))
    calls: list[ChatCompletionMessageFunctionToolCallParam] = [
        {
            "id": b.id,
            "type": "function",
            "function": {"name": b.name, "arguments": json.dumps(b.input)},
        }
        for b in message.content
        if isinstance(b, ToolUseBlock)
    ]
    if not calls:
        return {"role": "assistant", "content": text}
    # Chat completions wants null content when the turn is only tool calls.
    return {"role": "assistant", "content": text or None, "tool_calls": calls}


def _user_messages(message: Message) -> list[ChatCompletionMessageParam]:
    # Tool messages must follow the assistant turn that made the calls, so they
    # go first. Any user text is folded into one user message after them.
    out: list[ChatCompletionMessageParam] = []
    texts: list[str] = []
    for block in message.content:
        if isinstance(block, ToolResultBlock):
            out.append(
                {"role": "tool", "tool_call_id": block.tool_use_id, "content": _result_text(block)}
            )
        elif isinstance(block, TextBlock):
            texts.append(block.text)
        else:
            # A transcript bug is the same on every attempt, so it is not retryable.
            raise ProviderError(
                ProviderErrorKind.INVALID_REQUEST, "tool_use blocks belong to assistant messages"
            )
    if texts:
        out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _result_text(block: ToolResultBlock) -> str:
    return f"ERROR: {block.content}" if block.is_error else block.content


# ---------- chat completions -> contracts ----------
def _to_response(completion: object, *, model: str, latency_ms: int) -> LLMResponse:
    # Whatever the server sends back, the caller sees a ProviderError or a response.
    # LH-204 retries by ProviderError kind and would treat any other exception as
    # a bug in the activity.
    if not isinstance(completion, ChatCompletion):
        # A gateway answered 200 with a page or an error object. The SDK hands back
        # the raw body when it is not a chat completion.
        raise ProviderError(
            ProviderErrorKind.SERVER, f"response was not a chat completion: {_snippet(completion)}"
        )
    try:
        return _convert(completion, model=model, latency_ms=latency_ms)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        # The SDK builds models without checking them, so a field that a server
        # left out or mistyped fails here, not in the SDK.
        raise ProviderError(ProviderErrorKind.SERVER, f"unexpected response shape: {exc}") from exc


def _convert(completion: ChatCompletion, *, model: str, latency_ms: int) -> LLMResponse:
    if not completion.choices:
        raise ProviderError(ProviderErrorKind.SERVER, "response contained no choices")
    choice = completion.choices[0]
    message = choice.message
    if message is None:
        raise ProviderError(ProviderErrorKind.SERVER, "response choice had no message")
    content: list[Block] = []
    tool_calls: list[ToolCall] = []
    if message.content:
        content.append(TextBlock(text=message.content))
    if message.refusal:
        # The model declined. Keep the reason where the transcript and report show it.
        content.append(TextBlock(text=message.refusal))
    for raw in message.tool_calls or []:
        # Custom (non-function) tool calls are not part of the contract.
        if not isinstance(raw, ChatCompletionMessageFunctionToolCall):
            continue
        call = _tool_call(raw)
        tool_calls.append(call)
        content.append(ToolUseBlock(id=call.id, name=call.name, input=call.input))
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        stop_reason=_stop_reason(
            choice.finish_reason, has_calls=bool(tool_calls), refused=bool(message.refusal)
        ),
        # The SDK does not check types, so a server's odd finish_reason is dropped, not raised.
        raw_stop_reason=choice.finish_reason if isinstance(choice.finish_reason, str) else None,
        usage=_usage(completion.usage),
        model=model,
        latency_ms=latency_ms,
        request_id=_request_id(completion),
    )


def _request_id(completion: ChatCompletion) -> str | None:
    # The SDK copies the x-request-id header onto the parsed response.
    value = completion._request_id
    return value if isinstance(value, str) and value else None


def _snippet(body: object) -> str:
    text = body if isinstance(body, str) else repr(body)
    return f"{type(body).__name__}: {text[:_SNIPPET_CHARS]}"


def _stop_reason(finish_reason: str | None, *, has_calls: bool, refused: bool) -> _StopReason:
    if refused:
        return "other"
    mapped = _STOP_REASONS.get(finish_reason or "", "other")
    # Some servers send finish_reason "stop" with tool calls in place, and OpenAI
    # does when tool_choice forces a function. ScriptedProvider reports tool_use
    # whenever calls are present, so the loop sees one rule. A reply cut off by the
    # token cap stays max_tokens, because its last call may be half written.
    if has_calls and mapped != "max_tokens":
        return "tool_use"
    return mapped


def _tool_call(raw: ChatCompletionMessageFunctionToolCall) -> ToolCall:
    function = raw.function
    if not raw.id or function is None or not function.name:
        raise ProviderError(ProviderErrorKind.SERVER, "tool call is missing its id or name")
    return ToolCall(id=raw.id, name=function.name, input=_parse_arguments(function.arguments))


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    # A call that takes no arguments may arrive as "", blank or null. Parsing that
    # as JSON would fail, and the "_raw" entry would then break the strict tool schemas.
    if raw is None or not raw.strip():
        return {}
    # Models also emit invalid JSON, for example cut off at the token cap. Keep the
    # text under "_raw" so the transcript still shows what was sent and the turn
    # does not fail. The tool registry reports the key back to the model.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": raw}


def _usage(raw: CompletionUsage | None) -> Usage:
    if raw is None:
        return Usage()
    details = raw.prompt_tokens_details
    cached = _count(details.cached_tokens if details is not None else None)
    return Usage(
        input_tokens=max(_count(raw.prompt_tokens) - cached, 0),
        output_tokens=_count(raw.completion_tokens),
        cache_read_tokens=cached,
    )


def _count(value: Any) -> int:
    # Some servers leave a counter out or send null. Zero keeps a billed turn usable.
    return int(value or 0)


# ---------- errors ----------
def _map_error(exc: openai.APIError) -> ProviderError:
    message = exc.message
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError(ProviderErrorKind.TIMEOUT, message)
    if not isinstance(exc, openai.APIStatusError):
        # Connection failures and malformed bodies: treat as a server-side problem.
        return ProviderError(ProviderErrorKind.SERVER, message)

    error = ProviderError(_honour_should_retry(exc, _status_kind(exc)), message)
    if error.retryable:
        # Any transient status can carry the hint, a 503 or 529 as well as a 429.
        error.retry_after_s = _retry_after(exc)
    return error


def _status_kind(exc: openai.APIStatusError) -> ProviderErrorKind:
    # With SDK retries off, this is the only place that decides what is transient.
    # It has to cover the statuses the SDK itself would have retried: 408, 409,
    # 429 and 5xx.
    status = exc.status_code
    text = _error_text(exc)
    if status == 429:
        return _rate_limit_kind(exc, text)
    if status in (401, 403):
        return ProviderErrorKind.AUTH
    if status == 413:
        return ProviderErrorKind.CONTEXT_TOO_LONG
    if status == 408:
        return ProviderErrorKind.TIMEOUT
    if status == _OVERLOADED_STATUS or (status == 503 and _OVERLOADED_503_TEXT in text):
        return ProviderErrorKind.OVERLOADED
    if status >= 500 or status == 409:
        return ProviderErrorKind.SERVER
    # Checked for client errors only. A 5xx stays retryable whatever its text says.
    if _is_context_error(text):
        return ProviderErrorKind.CONTEXT_TOO_LONG
    return ProviderErrorKind.INVALID_REQUEST


def _rate_limit_kind(exc: openai.APIStatusError, text: str) -> ProviderErrorKind:
    # OpenAI uses 429 for two cases that no retry can clear.
    if "insufficient_quota" in (exc.code, exc.type):
        # Out of credit or at the monthly cap: a billing problem, not a rate limit.
        return ProviderErrorKind.AUTH
    if _REQUEST_TOO_LARGE in text:
        # One request is bigger than the tokens-per-minute limit. Only a smaller
        # transcript helps, which is what forced compaction produces.
        return ProviderErrorKind.CONTEXT_TOO_LONG
    return ProviderErrorKind.RATE_LIMIT


def _honour_should_retry(exc: openai.APIStatusError, kind: ProviderErrorKind) -> ProviderErrorKind:
    # The SDK obeys this header over the status code, so keep doing that. It moves
    # only the generic outcomes. AUTH and CONTEXT_TOO_LONG tell the caller something
    # specific, and a retry hint does not change that.
    hint = exc.response.headers.get("x-should-retry", "").strip().lower()
    if hint == "true" and kind == ProviderErrorKind.INVALID_REQUEST:
        return ProviderErrorKind.SERVER
    if hint == "false" and kind in _TRANSIENT_KINDS:
        return ProviderErrorKind.INVALID_REQUEST
    return kind


def _error_text(exc: openai.APIStatusError) -> str:
    # exc.message holds error.message and nothing for a non-JSON body or another
    # JSON shape, so add the body the SDK kept: a string, a dict or a list.
    return f"{exc.message} {exc.body}".lower()


def _retry_after(exc: openai.APIStatusError) -> float | None:
    # Same order as the SDK: milliseconds, seconds, then an HTTP date. A form that
    # gives nothing usable falls through to the next one. None sends the durable
    # layer back to its own backoff.
    headers = exc.response.headers
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


def _is_context_error(text: str) -> bool:
    return any(marker in text for marker in _CONTEXT_MARKERS)


# ---------- token estimate ----------
def _block_chars(block: Block) -> int:
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolResultBlock):
        return len(block.content)
    return len(str(block.input))


def _estimate(system: str, messages: list[Message]) -> int:
    # Same rule as ScriptedProvider so tests and live runs agree on budgets.
    total = len(system) + sum(_block_chars(b) for m in messages for b in m.content)
    return total // _CHARS_PER_TOKEN
