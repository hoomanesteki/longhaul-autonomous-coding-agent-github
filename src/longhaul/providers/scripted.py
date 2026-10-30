"""Deterministic provider driven by a YAML script. No network, no API keys.

Script format::

    steps:
      - match:                       # optional
          role: coder                # system prompt's first line is "Role: coder"
          turn_index: 2              # assistant turns done, counting turns folded into notes
          last_tool_result_contains: "FAILED"
          user_text_contains: "Hint from"        # text of the last user message
          request_contains: "turn_offset="       # any text or tool result in the request
        respond:
          text: "optional text"
          tool_calls: [{name: edit_file, input: {path: a.py}}]
          stop_reason: tool_use      # default: tool_use with calls, else end_turn
          raise: overloaded          # a ProviderErrorKind value; replaces the reply
          retry_after_s: 2.0
          times: 2                   # raise for the first 2 identical requests only

Selection rule: the first step whose match conditions all hold wins. The
state is derived from the request alone, so the provider is stateless and
replaying the same transcript gives the same answer. A step without ``match``
is shorthand for ``match: {turn_index: N}``, where N is its position among the
steps that have no ``match`` (the first bare step is turn 0, the next is turn 1).

The role is read from the first line of the system prompt (``Role: coder``).
The prompt builder (LH-110) must put that line first.

Text matchers. ``last_tool_result_contains`` looks at the last tool_result block
of the final message. ``user_text_contains`` looks at the text blocks of the last
user message, which is how a compactor round or a hint from a human is told
apart. ``request_contains`` looks at every text block and tool result in the
request, such as the notes message that a compaction puts first. Tool inputs and
the system prompt are not searched.

Raise steps. A raise step without ``times`` answers every request that matches
it, so it models an outage. With ``times: N`` it answers the first N times it
sees the same request, then stops matching that request and the next matching
step answers. A different request has its own count. Counts live in the
provider instance, so a new instance starts again. Give a raise step with
``times`` an explicit ``match``: the next bare step belongs to the next turn, so
it would not answer the request that the spent raise stopped matching.

Turns. ``turn_index`` is the number of assistant turns completed in the
transcript, counting turns that a compaction folded into notes. A compacted
transcript says how many it folded in a line of its own in the first user message,
for example ``[longhaul:turn_offset=6]``. Write it with
``longhaul.providers.base.format_turn_offset(n)``, where n is the number of
assistant turns removed from the front of the transcript so far, summed over all
compactions. The provider adds n to the assistant messages that are left, so a
step keyed on turn 5 matches the same turn before and after a compaction. Without
the line the offset is 0.

Tool ids. Each id is ``toolu_<hash>_<n>``, where the hash covers the whole
request and n is the position of the call in the reply. A retried identical
request gets the same ids. Any other request gets different ones, so ids stay
unique inside a transcript, even one that was compacted, and across the
transcripts of different roles and tasks.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
from longhaul.providers.base import turn_index

_ROLE_PREFIX = "Role: "
_TAIL_CHARS = 500
_USER_TEXT_TAIL_CHARS = 200
_OUTPUT_TOKENS_PER_CALL = 50
_ID_HASH_CHARS = 16


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Match(_Strict):
    role: str | None = None
    turn_index: int | None = None
    last_tool_result_contains: str | None = Field(default=None, min_length=1)
    user_text_contains: str | None = Field(default=None, min_length=1)
    request_contains: str | None = Field(default=None, min_length=1)


class _CallSpec(_Strict):
    name: str
    input: dict[str, Any] = {}


class _Respond(_Strict):
    text: str | None = None
    tool_calls: list[_CallSpec] = []
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "other"] | None = (
        None
    )
    raise_: ProviderErrorKind | None = None
    retry_after_s: float | None = None
    times: int | None = Field(default=None, ge=1)

    def __init__(self, **data: Any) -> None:
        if "raise" in data:
            data["raise_"] = data.pop("raise")
        super().__init__(**data)


class _Step(_Strict):
    match: _Match | None = None
    respond: _Respond


class _Script(_Strict):
    steps: list[_Step]


def _load(path: Path) -> list[_Step]:
    raw = yaml.safe_load(path.read_text())
    try:
        return _Script.model_validate(raw).steps
    except ValidationError as exc:
        raise ValueError(f"invalid script {path}: {exc}") from exc


def _role_of(system: str) -> str | None:
    first = system.split("\n", 1)[0]
    return first[len(_ROLE_PREFIX) :].strip() if first.startswith(_ROLE_PREFIX) else None


def _last_tool_result(messages: list[Message]) -> str | None:
    if not messages or messages[-1].role != "user":
        return None
    results = [b.content for b in messages[-1].content if isinstance(b, ToolResultBlock)]
    return results[-1] if results else None


def _user_text(messages: list[Message]) -> str | None:
    """Text blocks of the last user message, or None when the request has no user message."""
    for message in reversed(messages):
        if message.role == "user":
            return "\n".join(b.text for b in message.content if isinstance(b, TextBlock))
    return None


def _request_text(messages: list[Message]) -> str:
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
            elif isinstance(block, ToolResultBlock):
                parts.append(block.content)
    return "\n".join(parts)


def _block_chars(block: Block) -> int:
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolResultBlock):
        return len(block.content)
    return len(str(block.input))


def _estimate(system: str, messages: list[Message]) -> int:
    total = len(system) + sum(_block_chars(b) for m in messages for b in m.content)
    return total // 4


def _fingerprint(model: str, system: str, messages: list[Message], tools: list[ToolSpec]) -> str:
    """Hash of everything the model is asked, in a canonical form.

    Blocks are dumped one by one so a block type added to the contracts later
    needs no change here.
    """
    request = {
        "model": model,
        "system": system,
        "messages": [
            {"role": m.role, "content": [b.model_dump(mode="json") for b in m.content]}
            for m in messages
        ],
        "tools": [t.model_dump(mode="json") for t in tools],
    }
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _contains(haystack: str | None, needle: str | None) -> bool:
    """A condition that is not set always holds. One that is set needs text that has it."""
    return needle is None or (haystack is not None and needle in haystack)


@dataclass(frozen=True)
class _Request:
    """What the matchers can see. It is derived from the request alone."""

    role: str | None
    role_line: str
    turn: int
    last_result: str | None
    user_text: str | None
    request_text: str
    fingerprint: str


class ScriptedProvider:
    name = "scripted"

    def __init__(self, script_path: Path) -> None:
        self._steps = _load(script_path)
        # Bare steps get an implicit turn_index equal to their rank among bare steps.
        bare = [i for i, step in enumerate(self._steps) if step.match is None]
        self._implicit = {index: rank for rank, index in enumerate(bare)}
        # How often each (step, request) pair has raised, for steps that set times.
        self._raised: dict[tuple[int, str], int] = {}

    def _request(
        self, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> _Request:
        return _Request(
            role=_role_of(system),
            role_line=system.split("\n", 1)[0][:80],
            turn=turn_index(messages),
            last_result=_last_tool_result(messages),
            user_text=_user_text(messages),
            request_text=_request_text(messages),
            fingerprint=_fingerprint(model, system, messages, tools),
        )

    def _matches(self, index: int, step: _Step, req: _Request) -> bool:
        m = step.match
        if m is None:
            return self._implicit[index] == req.turn
        return (
            (m.role is None or m.role == req.role)
            and (m.turn_index is None or m.turn_index == req.turn)
            and _contains(req.last_result, m.last_tool_result_contains)
            and _contains(req.user_text, m.user_text_contains)
            and _contains(req.request_text, m.request_contains)
        )

    def _spent(self, index: int, step: _Step, req: _Request) -> bool:
        times = step.respond.times
        return times is not None and self._raised.get((index, req.fingerprint), 0) >= times

    def _select(self, req: _Request) -> tuple[int, _Step]:
        spent: list[int] = []
        for index, step in enumerate(self._steps):
            if not self._matches(index, step, req):
                continue
            if self._spent(index, step, req):
                spent.append(index)
                continue
            return index, step
        raise self._no_match(req, spent)

    def _no_match(self, req: _Request, spent: list[int]) -> ProviderError:
        last = req.last_result
        tail = last[:_TAIL_CHARS] if last is not None else None
        user = req.user_text[-_USER_TEXT_TAIL_CHARS:] if req.user_text else None
        message = (
            f"no scripted step matches: turn_index={req.turn} role={req.role} "
            f"role_line={req.role_line!r} last_tool_result={tail!r} user_text={user!r}"
        )
        if spent:
            message += f"; raise steps {spent} already raised as often as times allows"
        return ProviderError(ProviderErrorKind.INVALID_REQUEST, message)

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
        req = self._request(model, system, messages, tools)
        index, step = self._select(req)
        resp = step.respond
        if resp.raise_ is not None:
            if resp.times is not None:
                key = (index, req.fingerprint)
                self._raised[key] = self._raised.get(key, 0) + 1
            raise ProviderError(
                resp.raise_, f"scripted {resp.raise_.value} error", resp.retry_after_s
            )
        digest = req.fingerprint[:_ID_HASH_CHARS]
        calls = [
            ToolCall(id=f"toolu_{digest}_{n}", name=c.name, input=c.input)
            for n, c in enumerate(resp.tool_calls)
        ]
        content: list[Block] = []
        if resp.text is not None:
            content.append(TextBlock(text=resp.text))
        content.extend(ToolUseBlock(id=c.id, name=c.name, input=c.input) for c in calls)
        usage = Usage(
            input_tokens=_estimate(system, messages),
            output_tokens=_OUTPUT_TOKENS_PER_CALL * len(calls) + len(resp.text or "") // 4,
        )
        return LLMResponse(
            content=content,
            tool_calls=calls,
            stop_reason=resp.stop_reason or ("tool_use" if calls else "end_turn"),
            usage=usage,
            model=model,
            latency_ms=0,
        )

    async def count_tokens(
        self, *, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> int:
        return _estimate(system, messages)
