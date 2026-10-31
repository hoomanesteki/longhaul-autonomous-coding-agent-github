"""Turn functions: one model call, one tool call, and the transcript edits around them.

The in-process loop (loop.py) calls these directly. Phase 2 wraps `llm_step` and `tool_step`
as Temporal activities without changing them, so nothing here reads a clock or keeps state
between calls. Every edit returns a new list and leaves its argument alone, because an
activity can be retried with the same input and the retry has to see what the first try saw.

The pairing rule. The Messages API answers 400 when a tool_use has no tool_result in the next
user message, when a tool_result has no tool_use, and when text comes before a tool_result.
A 400 is never retried, so any of them would end the task. The edits keep the rule by
construction and `validate_pairs` checks it (D9):

- every tool_use gets exactly one tool_result, in call order, in the one user message that
  follows it;
- a control tool, which the loop handles itself and never runs, gets a short ok result;
- a call that did not run gets an error result that says so;
- harness text, such as a nudge, goes after the results and never in front of them.
"""

from longhaul.contracts import (
    Block,
    CallOptions,
    LLMProvider,
    LLMResponse,
    Message,
    Sandbox,
    TextBlock,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultBlock,
    ToolSpec,
    ToolUseBlock,
)
from longhaul.tools.control import is_control_tool
from longhaul.tools.registry import ToolRegistry

CONTROL_RESULT = "ok"
DEFAULT_NOT_RUN_REASON = "no result was recorded for this call"


class TranscriptError(ValueError):
    """A transcript, or an edit to one, that the Messages API would answer with a 400."""


# ---------- the two steps ----------
async def llm_step(
    provider: LLMProvider,
    model: str,
    system: str,
    transcript: list[Message],
    tools: list[ToolSpec],
    max_tokens: int,
    options: CallOptions | None = None,
) -> LLMResponse:
    """One model call. A ProviderError goes up as it is, for the caller's retry policy."""
    return await provider.complete(
        model=model,
        system=system,
        messages=transcript,
        tools=tools,
        max_tokens=max_tokens,
        options=options,
    )


async def tool_step(
    registry: ToolRegistry, sandbox: Sandbox, call: ToolCall, ctx: ToolContext
) -> ToolResult:
    """Run one call. Input the model got wrong comes back as an ok=False result.

    Anything else, such as a sandbox that is gone, is not the model's doing and propagates.
    """
    return await registry.run(call, sandbox, ctx)


# ---------- transcript edits ----------
def append_assistant(transcript: list[Message], resp: LLMResponse) -> list[Message]:
    """Add the model's reply, or leave the transcript as it is when the reply is empty.

    A reply with no text and no tool_use is not stored: the API answers 400 to an assistant
    message with nothing in it, and render.py would skip it and leave two user messages side
    by side. Blank text blocks are dropped for the same reason.

    The stop reason is read before the content (D9). Calls run only after a tool_use stop, so
    tool_use blocks are kept only then. In any other reply, such as one cut off at max_tokens,
    they would never get a result, and the API answers 400 to a tool_use with none.
    """
    keep_calls = resp.stop_reason == "tool_use"
    blocks = [
        b
        for b in resp.content
        if not _is_blank_text(b) and (keep_calls or not isinstance(b, ToolUseBlock))
    ]
    if not any(isinstance(b, TextBlock | ToolUseBlock) for b in blocks):
        return list(transcript)
    if not transcript or transcript[-1].role != "user":
        raise TranscriptError("an assistant message has to follow a user message")
    return [*transcript, Message(role="assistant", content=blocks)]


def append_tool_results(
    transcript: list[Message],
    calls: list[ToolCall],
    results: list[ToolResult],
    *,
    not_run_reason: str = DEFAULT_NOT_RUN_REASON,
) -> list[Message]:
    """Answer the tool_use blocks of the last assistant message in one new user message.

    `calls` are those blocks in order, and `results` the results of the ones that ran, matched
    by call id and given in any order. The new message holds one tool_result per call in call
    order. A control tool without a result gets a short ok one. Any other call without a result
    gets an error result, "not run: <not_run_reason>".
    """
    if not calls:
        return list(transcript)
    _require_calls_in_last_message(transcript, calls)
    by_id = _results_by_call(calls, results)
    blocks: list[Block] = [_result_block(c, by_id.get(c.id), not_run_reason) for c in calls]
    return [*transcript, Message(role="user", content=blocks)]


def append_user_text(transcript: list[Message], text: str) -> list[Message]:
    """Add text from the harness, such as a nudge, where the API accepts it.

    After a user message it becomes a text block at the end of that message, so after the tool
    results when it holds some. After an assistant message with no tool_use it is a new user
    message. It never goes ahead of tool results: the API takes the results first, and Sonnet
    5.5 may ignore text that sits inside a tool_result.
    """
    if not text.strip():
        raise ValueError("user text must not be blank")
    block = TextBlock(text=text)
    last = transcript[-1] if transcript else None
    if last is not None and last.role == "user":
        merged = Message(role="user", content=[*last.content, block])
        return [*transcript[:-1], merged]
    if last is not None and _tool_use_ids(last):
        raise TranscriptError(
            "the last assistant message has tool_use blocks, so their tool_results have to "
            "be added before any text"
        )
    return [*transcript, Message(role="user", content=[block])]


def validate_pairs(transcript: list[Message]) -> None:
    """Raise TranscriptError unless every tool_use and tool_result is paired as D9 asks.

    Each assistant message with tool_use blocks is followed by a user message that opens with
    exactly one tool_result per block, in the same order. A message with tool_use but no
    results after it, results with nothing to answer, an id used twice, and text ahead of a
    result are all errors.
    """
    open_ids: list[str] = []  # tool_use ids from the previous message, still unanswered
    open_index = -1
    seen: set[str] = set()
    for index, message in enumerate(transcript):
        if message.role == "assistant":
            _check_assistant(index, message, open_ids, open_index, seen)
            open_ids, open_index = _tool_use_ids(message), index
        else:
            _check_user(index, message, open_ids, open_index)
            open_ids = []
    if open_ids:
        raise TranscriptError(
            f"message {open_index} (assistant) has tool_use {open_ids} with no tool_result after it"
        )


# ---------- helpers ----------
def _is_blank_text(block: Block) -> bool:
    return isinstance(block, TextBlock) and not block.text.strip()


def _tool_use_ids(message: Message) -> list[str]:
    return [b.id for b in message.content if isinstance(b, ToolUseBlock)]


def _tool_result_ids(message: Message) -> list[str]:
    return [b.tool_use_id for b in message.content if isinstance(b, ToolResultBlock)]


def _require_calls_in_last_message(transcript: list[Message], calls: list[ToolCall]) -> None:
    wanted = [c.id for c in calls]
    last = transcript[-1] if transcript else None
    have = _tool_use_ids(last) if last is not None and last.role == "assistant" else []
    if have != wanted:
        raise TranscriptError(
            f"the calls are {wanted}, but the last message of the transcript has tool_use {have}"
        )


def _results_by_call(calls: list[ToolCall], results: list[ToolResult]) -> dict[str, ToolResult]:
    known = {c.id for c in calls}
    by_id: dict[str, ToolResult] = {}
    for r in results:
        if r.call_id not in known:
            raise TranscriptError(
                f"tool result for {r.call_id!r}, which is not a call in this turn"
            )
        if r.call_id in by_id:
            raise TranscriptError(f"two tool results for {r.call_id!r}")
        by_id[r.call_id] = r
    return by_id


def _result_block(call: ToolCall, result: ToolResult | None, not_run_reason: str) -> Block:
    if result is not None:
        return ToolResultBlock(tool_use_id=call.id, content=result.output, is_error=not result.ok)
    if is_control_tool(call.name):
        return ToolResultBlock(tool_use_id=call.id, content=CONTROL_RESULT)
    return ToolResultBlock(tool_use_id=call.id, content=f"not run: {not_run_reason}", is_error=True)


def _check_assistant(
    index: int, message: Message, open_ids: list[str], open_index: int, seen: set[str]
) -> None:
    if open_ids:
        raise TranscriptError(
            f"message {open_index} (assistant) has tool_use {open_ids} with no tool_result "
            f"after it, and message {index} is another assistant message"
        )
    if _tool_result_ids(message):
        raise TranscriptError(f"message {index} is an assistant message with a tool_result block")
    ids = _tool_use_ids(message)
    reused = sorted({i for i in ids if i in seen or ids.count(i) > 1})
    if reused:
        raise TranscriptError(f"message {index} (assistant) reuses tool_use ids {reused}")
    seen.update(ids)


def _check_user(index: int, message: Message, open_ids: list[str], open_index: int) -> None:
    if _tool_use_ids(message):
        raise TranscriptError(f"message {index} is a user message with a tool_use block")
    got = _tool_result_ids(message)
    if got != open_ids:
        raise TranscriptError(_mismatch(index, got, open_ids, open_index))
    if not all(isinstance(b, ToolResultBlock) for b in message.content[: len(got)]):
        raise TranscriptError(
            f"message {index} (user) has text before a tool_result, and the tool_result "
            "blocks have to come first"
        )


def _mismatch(index: int, got: list[str], open_ids: list[str], open_index: int) -> str:
    repeated = sorted({i for i in got if got.count(i) > 1})
    missing = [i for i in open_ids if i not in got]
    extra = [i for i in got if i not in open_ids]
    problems = []
    if repeated:
        problems.append(f"more than one tool_result for {repeated}")
    if missing:
        problems.append(f"no tool_result for tool_use {missing}")
    if extra:
        problems.append(f"tool_result {extra} with no tool_use to answer")
    if not problems:
        problems.append(f"tool_results out of call order: expected {open_ids}, got {got}")
    before = f"message {open_index}" if open_ids else "the message before it"
    return f"message {index} (user) does not answer {before}: " + "; ".join(problems)
