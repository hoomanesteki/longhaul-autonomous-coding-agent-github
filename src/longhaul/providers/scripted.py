"""Deterministic provider driven by a YAML script. No network, no API keys.

Script format::

    steps:
      - match:                       # optional
          role: coder                # system prompt's first line is "Role: coder"
          turn_index: 2              # assistant turns done, counting turns folded into notes
          last_tool_result_contains: "FAILED"
        respond:
          text: "optional text"
          tool_calls: [{name: edit_file, input: {path: a.py}}]
          stop_reason: tool_use      # default: tool_use with calls, else end_turn
          raise: overloaded          # a ProviderErrorKind value; replaces the reply
          retry_after_s: 2.0

Selection rule: the first step whose match conditions all hold wins. The
state is derived from the request alone, so the provider is stateless and
replaying the same transcript gives the same answer. A step without ``match``
is shorthand for ``match: {turn_index: N}``, where N is its position among the
steps that have no ``match`` (the first bare step is turn 0, the next is turn 1).

The role is read from the first line of the system prompt (``Role: coder``).
The prompt builder (LH-110) must put that line first.

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
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

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
_OUTPUT_TOKENS_PER_CALL = 50
_ID_HASH_CHARS = 16


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Match(_Strict):
    role: str | None = None
    turn_index: int | None = None
    last_tool_result_contains: str | None = None


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


class ScriptedProvider:
    name = "scripted"

    def __init__(self, script_path: Path) -> None:
        self._steps = _load(script_path)
        # Bare steps get an implicit turn_index equal to their rank among bare steps.
        self._implicit: dict[int, int] = {}
        for i, step in enumerate(s for s in self._steps if s.match is None):
            self._implicit[id(step)] = i

    def _matches(self, step: _Step, role: str | None, turn: int, last: str | None) -> bool:
        if step.match is None:
            return self._implicit[id(step)] == turn
        m = step.match
        if m.role is not None and m.role != role:
            return False
        if m.turn_index is not None and m.turn_index != turn:
            return False
        if m.last_tool_result_contains is not None:
            return last is not None and m.last_tool_result_contains in last
        return True

    def _select(self, system: str, messages: list[Message]) -> _Step:
        role, turn, last = _role_of(system), turn_index(messages), _last_tool_result(messages)
        for step in self._steps:
            if self._matches(step, role, turn, last):
                return step
        tail = last[:_TAIL_CHARS] if last is not None else None
        raise ProviderError(
            ProviderErrorKind.INVALID_REQUEST,
            f"no scripted step matches: turn_index={turn} role={role} "
            f"role_line={system.split(chr(10), 1)[0][:80]!r} last_tool_result={tail!r}",
        )

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
        step = self._select(system, messages)
        resp = step.respond
        if resp.raise_ is not None:
            raise ProviderError(
                resp.raise_, f"scripted {resp.raise_.value} error", resp.retry_after_s
            )
        digest = _fingerprint(model, system, messages, tools)[:_ID_HASH_CHARS]
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
