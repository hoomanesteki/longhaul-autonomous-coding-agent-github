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
The format is ``longhaul.providers.base.ROLE_LINE_FORMAT``, and prompt builders
must start with it. ``match.role`` must be a ``Role`` value, which is checked when
the script loads. If any step names a role, a prompt without a role line fails with
INVALID_REQUEST instead of falling through to a step for another role. For the same
reason, a script with role-keyed steps cannot hold bare steps: a bare step answers
every role, so give such a step an explicit ``match: {role: ...}``.

A script is checked when it loads, and a bad one raises ValueError with the file
path. A key may not appear twice in one mapping. A reply needs text or tool calls.
A raise cannot come with a reply, and retry_after_s and times only go with a raise.
stop_reason has to fit the calls: tool_use needs one, end_turn allows none.

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

Tools. A reply may only call tools that the request offered, because a model
cannot call any others. A scripted call to a tool that was not offered fails
with INVALID_REQUEST. The tool definitions count as input tokens, like they do
for a real provider: the usage rule is the characters of the system prompt, the
blocks and each tool's name, description and JSON schema, divided by 4.

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

import copy
import hashlib
import json
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from longhaul.contracts import (
    Block,
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
    Role,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
    Usage,
)
from longhaul.providers.base import (
    ROLE_LINE_FORMAT,
    format_role_line,
    parse_role_line,
    turn_index,
)

_TAIL_CHARS = 500
_USER_TEXT_TAIL_CHARS = 200
_OUTPUT_TOKENS_PER_CALL = 50
_ID_HASH_CHARS = 16
_MERGE_TAG = "tag:yaml.org,2002:merge"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Match(_Strict):
    role: Role | None = None
    turn_index: int | None = Field(default=None, ge=0)
    last_tool_result_contains: str | None = Field(default=None, min_length=1)
    user_text_contains: str | None = Field(default=None, min_length=1)
    request_contains: str | None = Field(default=None, min_length=1)


class _CallSpec(_Strict):
    name: str = Field(min_length=1)
    input: dict[str, Any] = {}


class _Respond(_Strict):
    text: str | None = None
    tool_calls: list[_CallSpec] = []
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "other"] | None = (
        None
    )
    raise_: ProviderErrorKind | None = Field(default=None, alias="raise")
    retry_after_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    times: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _only_responses_a_provider_can_give(self) -> Self:
        if self.raise_ is not None:
            self._check_raise_step()
        else:
            self._check_reply()
        return self

    def _check_raise_step(self) -> None:
        set_too = [
            name
            for name, present in (
                ("text", self.text is not None),
                ("tool_calls", bool(self.tool_calls)),
                ("stop_reason", self.stop_reason is not None),
            )
            if present
        ]
        if set_too:
            raise ValueError(f"raise replaces the reply, so it cannot come with {set_too}")

    def _check_reply(self) -> None:
        for name in ("retry_after_s", "times"):
            if getattr(self, name) is not None:
                raise ValueError(f"{name} only applies to a raise step")
        if self.text is None and not self.tool_calls:
            raise ValueError("respond is empty: give text, tool_calls or raise")
        if self.stop_reason == "tool_use" and not self.tool_calls:
            raise ValueError("stop_reason tool_use needs a tool call")
        if self.stop_reason == "end_turn" and self.tool_calls:
            raise ValueError("stop_reason end_turn cannot come with tool calls")


class _Step(_Strict):
    match: _Match | None = None
    respond: _Respond


class _Script(_Strict):
    steps: list[_Step]

    @model_validator(mode="after")
    def _role_keyed_scripts_have_no_bare_steps(self) -> Self:
        # A bare step answers every role, so next to role-keyed steps it would
        # answer silently for a role it was not written for.
        keyed = any(s.match is not None and s.match.role is not None for s in self.steps)
        bare = [i for i, s in enumerate(self.steps) if s.match is None]
        if keyed and bare:
            raise ValueError(
                f"steps {bare} have no match, so they would answer every role, but other "
                "steps are keyed by role; add match: {role: ...} to them"
            )
        return self


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects a key written twice in one mapping.

    PyYAML keeps the last value without a word, so a second ``respond:`` in a
    step would replace the first and the script would not say what it looks like.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Hashable, Any]:
        seen: set[Hashable] = set()
        for key_node, _ in node.value:
            if key_node.tag == _MERGE_TAG:
                continue
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, Hashable):
                continue  # the base class reports it
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load(path: Path) -> list[_Step]:
    try:
        raw = yaml.load(path.read_text(), Loader=_UniqueKeyLoader)
        return _Script.model_validate(raw).steps
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"invalid script {path}: {exc}") from exc


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
    # Block types that are not listed here, such as thinking blocks, add no characters.
    if isinstance(block, TextBlock):
        return len(block.text)
    if isinstance(block, ToolResultBlock):
        return len(block.content)
    if isinstance(block, ToolUseBlock):
        return len(str(block.input))
    return 0


def _spec_chars(spec: ToolSpec) -> int:
    return len(spec.name) + len(spec.description) + len(json.dumps(spec.input_schema, default=str))


def _estimate(system: str, messages: list[Message], tools: list[ToolSpec]) -> int:
    # Real providers count the tool definitions as input, so the synthetic count does too.
    total = len(system) + sum(_block_chars(b) for m in messages for b in m.content)
    return (total + sum(_spec_chars(t) for t in tools)) // 4


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
        self.script_path = script_path
        self._steps = _load(script_path)
        # Bare steps get an implicit turn_index equal to their rank among bare steps.
        bare = [i for i, step in enumerate(self._steps) if step.match is None]
        self._implicit = {index: rank for rank, index in enumerate(bare)}
        self._role_keyed = any(
            s.match is not None and s.match.role is not None for s in self._steps
        )
        # How often each (step, request) pair has raised, for steps that set times.
        self._raised: dict[tuple[int, str], int] = {}

    def _request(
        self, model: str, system: str, messages: list[Message], tools: list[ToolSpec]
    ) -> _Request:
        return _Request(
            role=parse_role_line(system),
            role_line=system.split("\n", 1)[0][:80],
            turn=turn_index(messages),
            last_result=_last_tool_result(messages),
            user_text=_user_text(messages),
            request_text=_request_text(messages),
            fingerprint=_fingerprint(model, system, messages, tools),
        )

    def _require_role_line(self, req: _Request) -> None:
        """Role-keyed steps cannot match a prompt with no role line, so say that, not 'no match'."""
        if self._role_keyed and req.role is None:
            raise ProviderError(
                ProviderErrorKind.INVALID_REQUEST,
                "the script keys steps by role, so the system prompt must start with a role "
                f"line, {ROLE_LINE_FORMAT!r}, such as {format_role_line(Role.CODER)!r}; "
                f"its first line is {req.role_line!r}",
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
        tail = last[-_TAIL_CHARS:] if last is not None else None
        user = req.user_text[-_USER_TEXT_TAIL_CHARS:] if req.user_text else None
        message = (
            f"no scripted step matches: turn_index={req.turn} role={req.role} "
            f"role_line={req.role_line!r} last_tool_result={tail!r} user_text={user!r}"
        )
        if spent:
            message += f"; raise steps {spent} already raised as often as times allows"
        return ProviderError(ProviderErrorKind.INVALID_REQUEST, message)

    @staticmethod
    def _check_offered(index: int, resp: _Respond, tools: list[ToolSpec]) -> None:
        """A model can only call the tools in the request, so a script may not either."""
        offered = sorted(t.name for t in tools)
        for call in resp.tool_calls:
            if call.name not in offered:
                raise ProviderError(
                    ProviderErrorKind.INVALID_REQUEST,
                    f"scripted step {index} calls {call.name!r}, which was not offered; "
                    f"offered tools: {offered}",
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
        req = self._request(model, system, messages, tools)
        self._require_role_line(req)
        index, step = self._select(req)
        resp = step.respond
        if resp.raise_ is not None:
            if resp.times is not None:
                key = (index, req.fingerprint)
                self._raised[key] = self._raised.get(key, 0) + 1
            raise ProviderError(
                resp.raise_, f"scripted {resp.raise_.value} error", resp.retry_after_s
            )
        self._check_offered(index, resp, tools)
        digest = req.fingerprint[:_ID_HASH_CHARS]
        # Pydantic copies only the top level of an input. Nested values are copied here so a
        # consumer that edits its arguments in place cannot change the script.
        calls = [
            ToolCall(id=f"toolu_{digest}_{n}", name=c.name, input=copy.deepcopy(c.input))
            for n, c in enumerate(resp.tool_calls)
        ]
        content: list[Block] = []
        if resp.text is not None:
            content.append(TextBlock(text=resp.text))
        content.extend(
            ToolUseBlock(id=c.id, name=c.name, input=copy.deepcopy(c.input)) for c in calls
        )
        usage = Usage(
            input_tokens=_estimate(system, messages, tools),
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
        return _estimate(system, messages, tools)
