"""Provider for any endpoint that speaks the OpenAI chat completions format.

The openai SDK does the HTTP. This module converts between contracts and the
chat completions shape, and maps SDK errors to ProviderErrorKind. Retries are
off (max_retries=0) because the durable layer owns them.

cache_system is accepted for protocol compatibility and ignored. Chat
completions has no cache_control marker; caching, when the endpoint does it,
happens on the server side.
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

# Keys are the chat completions finish_reason values.
_STOP_REASONS: dict[str, _StopReason] = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "length": "max_tokens",
}
_CONTEXT_CODE = "context_length_exceeded"
_CONTEXT_MESSAGE = "maximum context length"
_OVERLOADED_STATUS = 529
_OVERLOADED_503_TEXT = "overloaded"
_CHARS_PER_TOKEN = 4


class OpenAICompatProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        client: AsyncOpenAI | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        # A prebuilt client wins. Tests pass an http_client backed by MockTransport.
        if client is None:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url,
                max_retries=0,
                http_client=http_client,
            )
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
                max_tokens=max_tokens,
            )
        except openai.APIError as exc:
            raise _map_error(exc) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        return _to_response(completion, model=model, latency_ms=latency_ms)

    async def count_tokens(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> int:
        return _estimate(system, messages)


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
    cached = details.cached_tokens if details is not None else None
    return Usage(
        input_tokens=raw.prompt_tokens,
        output_tokens=raw.completion_tokens,
        cache_read_tokens=cached or 0,
    )


# ---------- errors ----------
def _map_error(exc: openai.APIError) -> ProviderError:
    message = exc.message
    if isinstance(exc, openai.APITimeoutError):
        return ProviderError(ProviderErrorKind.TIMEOUT, message)
    if not isinstance(exc, openai.APIStatusError):
        # Connection failures and malformed bodies: treat as a server-side problem.
        return ProviderError(ProviderErrorKind.SERVER, message)

    status = exc.status_code
    lowered = message.lower()
    if isinstance(exc, openai.RateLimitError) or status == 429:
        return ProviderError(ProviderErrorKind.RATE_LIMIT, message, _retry_after(exc))
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return ProviderError(ProviderErrorKind.AUTH, message)
    if status == _OVERLOADED_STATUS or (status == 503 and _OVERLOADED_503_TEXT in lowered):
        return ProviderError(ProviderErrorKind.OVERLOADED, message)
    if status >= 500:
        return ProviderError(ProviderErrorKind.SERVER, message)
    if _is_context_error(exc):
        return ProviderError(ProviderErrorKind.CONTEXT_TOO_LONG, message)
    return ProviderError(ProviderErrorKind.INVALID_REQUEST, message)


def _retry_after(exc: openai.APIStatusError) -> float | None:
    raw = exc.response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        # The HTTP-date form is not parsed. The durable layer falls back to its own backoff.
        return None


def _is_context_error(exc: openai.APIStatusError) -> bool:
    return (exc.code or "") == _CONTEXT_CODE or _CONTEXT_MESSAGE in exc.message.lower()


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
