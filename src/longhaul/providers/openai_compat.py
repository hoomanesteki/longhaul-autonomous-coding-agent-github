"""Provider for any endpoint that speaks the OpenAI chat completions format.

The openai SDK does the HTTP. This module converts between contracts and the
chat completions shape, and maps SDK errors to ProviderErrorKind. Retries are
off (max_retries=0) because the durable layer owns them.

cache_system is accepted for protocol compatibility and ignored. Chat
completions has no cache_control marker; caching, when the endpoint does it,
happens on the server side.

The reply cap is sent as max_completion_tokens to OpenAI's own endpoint and as
max_tokens to every other server. Pass max_tokens_param to override the choice.

Usage buckets do not overlap, as with the Anthropic provider: input_tokens
leaves out cache_read_tokens. Chat completions reports prompt_tokens with the
cached tokens inside it, so the cached part is taken out here. Cost (LH-701)
and cache_ratio (LH-405) add the buckets up and would count cached tokens twice
otherwise.
"""

import json
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
    ChatCompletionToolUnionParam,
)
from openai.types.completion_usage import CompletionUsage

from longhaul.contracts import (
    Block,
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
_OVERLOADED_STATUS = 529
_OVERLOADED_503_TEXT = "overloaded"
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
    ) -> LLMResponse:
        chat_tools: list[ChatCompletionToolUnionParam] | Omit = (
            [_tool_param(t) for t in tools] if tools else omit
        )
        started = time.monotonic()
        try:
            completion = await self._client.chat.completions.create(
                model=model,
                messages=_chat_messages(system, messages),
                tools=chat_tools,
                max_tokens=max_tokens if self._max_tokens_param == "max_tokens" else omit,
                max_completion_tokens=(
                    max_tokens if self._max_tokens_param == "max_completion_tokens" else omit
                ),
            )
        except openai.APIError as exc:
            raise _map_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        return _to_response(completion, model=model, latency_ms=latency_ms)

    async def count_tokens(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
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
            raise ValueError("tool_use blocks belong to assistant messages")
    if texts:
        out.append({"role": "user", "content": "\n".join(texts)})
    return out


def _result_text(block: ToolResultBlock) -> str:
    return f"ERROR: {block.content}" if block.is_error else block.content


# ---------- chat completions -> contracts ----------
def _to_response(completion: ChatCompletion, *, model: str, latency_ms: int) -> LLMResponse:
    if not completion.choices:
        raise ProviderError(ProviderErrorKind.SERVER, "response contained no choices")
    choice = completion.choices[0]
    content: list[Block] = []
    tool_calls: list[ToolCall] = []
    if choice.message.content:
        content.append(TextBlock(text=choice.message.content))
    for raw in choice.message.tool_calls or []:
        # Custom (non-function) tool calls are not part of the contract.
        if not isinstance(raw, ChatCompletionMessageFunctionToolCall):
            continue
        args = _parse_arguments(raw.function.arguments)
        tool_calls.append(ToolCall(id=raw.id, name=raw.function.name, input=args))
        content.append(ToolUseBlock(id=raw.id, name=raw.function.name, input=args))
    return LLMResponse(
        content=content,
        tool_calls=tool_calls,
        stop_reason=_STOP_REASONS.get(choice.finish_reason or "", "other"),
        usage=_usage(completion.usage),
        model=model,
        latency_ms=latency_ms,
    )


def _parse_arguments(raw: str) -> dict[str, Any]:
    # Models sometimes emit invalid JSON. Keep the raw text so the transcript
    # still shows what was sent, instead of failing the whole turn.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return parsed if isinstance(parsed, dict) else {"_raw": raw}


def _usage(raw: CompletionUsage | None) -> Usage:
    if raw is None:
        return Usage()
    details = raw.prompt_tokens_details
    cached = (details.cached_tokens if details is not None else None) or 0
    return Usage(
        input_tokens=max(raw.prompt_tokens - cached, 0),
        output_tokens=raw.completion_tokens,
        cache_read_tokens=cached,
    )


# ---------- errors ----------
def _map_error(exc: openai.APIError) -> ProviderError:
    message = exc.message
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError(ProviderErrorKind.TIMEOUT, message)
    if not isinstance(exc, openai.APIStatusError):
        # Connection failures and malformed bodies: treat as a server-side problem.
        return ProviderError(ProviderErrorKind.SERVER, message)

    kind = _honour_should_retry(exc, _status_kind(exc))
    retry_after = _retry_after(exc) if kind == ProviderErrorKind.RATE_LIMIT else None
    return ProviderError(kind, message, retry_after)


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
    raw = exc.response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # The HTTP-date form is not parsed. The durable layer falls back to its own backoff.
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
