"""The turn rules that the in-process loop and the durable workflow share.

Everything here is a decision with no I/O: what a stop reason leads to, when a turn is sampled
again, when a run ends, which calls run and how a finish is told from a bad one. The
in-process loop (loop.py) and TaskWorkflow call these functions, so the two cannot drift
apart. They differ only in how a model call or a tool call is made: a direct call in one, an
activity in the other.

The workflow loads this module inside Temporal's sandbox, so it imports the contracts and
nothing else (tests/unit/test_rules.py checks that, and the I1 scan in test_invariants.py
follows the import). Transcript edits live in turns.py, which the activities use, because
turns.py reaches the tool registry.

Stop reasons (D8, D9). The stop reason is read before any content. A reply that stopped on
max_tokens, a refusal or a full window is never stored and none of its tool calls run: a
tool_use cut off at max_tokens can be a valid but partial object, and a refusal may hold output
the model withdrew. Calls run only after a tool_use stop.

How a run ends. Finish is the one good end. Every other end is an ERROR outcome with a fixed
summary, so a caller can tell them apart without reading prose:

- the model calls `finish` with a summary: PR_OPENED with that summary;
- the turn limit passes: "turn limit";
- two turns in a row end without a tool call: "no tool calls". After the first one a nudge
  goes to the model;
- a turn stops on max_tokens, is retried once at the higher cap, and stops on it again:
  "output limit";
- the model refuses: "model declined (<category>): <explanation>";
- the context window fills, or the provider says the prompt is over it: "context limit";
- a turn is paused more than MAX_PAUSES times: "pause limit";
- a stop reason with no rule (stop_sequence, other): "unexpected stop reason: <reason>".
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, assert_never

from longhaul.contracts import (
    Block,
    LLMResponse,
    Outcome,
    OutcomeKind,
    ProviderErrorKind,
    StopDetails,
    StopReason,
    TextBlock,
    ToolResult,
)

# The `finish` control tool ends a task. The name is copied from longhaul.tools.control.FINISH,
# which this module cannot import without loading the tools package. A unit test keeps the two
# equal.
FINISH = "finish"

NUDGE = "Use tools or call finish."
MAX_PAUSES = 5
# Turns in a row that end without a tool call. The first gets the nudge, the second ends the run.
IDLE_LIMIT = 2

TURN_LIMIT = "turn limit"
OUTPUT_LIMIT = "output limit"
CONTEXT_LIMIT = "context limit"
PAUSE_LIMIT = "pause limit"
NO_TOOL_CALLS = "no tool calls"

BAD_FINISH = (
    "finish needs a summary: a short text that says what you changed. Call finish again with one."
)
SKIPPED_AFTER_FINISH = "finish was called earlier in this turn"


# ---------- outcomes ----------
def error(summary: str) -> Outcome:
    return Outcome(kind=OutcomeKind.ERROR, summary=summary)


def finished(summary: str) -> Outcome:
    """The one good end. No pull request exists yet, so there is no url (Phase 3 adds it)."""
    return Outcome(kind=OutcomeKind.PR_OPENED, summary=summary)


def turn_limit_outcome() -> Outcome:
    return error(TURN_LIMIT)


def provider_error_outcome(kind: ProviderErrorKind) -> Outcome | None:
    """The outcome a provider error ends the run with, or None when it is not the run's to end.

    A prompt over the window cannot be fixed by asking again. Compaction handles it from
    Phase 4 on; until then the run ends. Every other kind is for the retry policy of the caller.
    """
    return error(CONTEXT_LIMIT) if kind is ProviderErrorKind.CONTEXT_TOO_LONG else None


def declined(details: StopDetails | None) -> str:
    # The API can send a refusal with no details at all, or with one of the two missing.
    category = (details.category or "").strip() if details is not None else ""
    explanation = (details.explanation or "").strip() if details is not None else ""
    return f"model declined ({category or 'unknown'}): {explanation or 'no explanation given'}"


# ---------- one reply ----------
class StopView(Protocol):
    """What the rules read from a reply: why it stopped, never what it said.

    LLMResponse has these fields, and so does the summary an llm_turn activity returns, which
    is all the workflow sees of a reply.
    """

    @property
    def stop_reason(self) -> StopReason: ...

    @property
    def stop_details(self) -> StopDetails | None: ...

    @property
    def raw_stop_reason(self) -> str | None: ...


@dataclass(frozen=True)
class Sampling:
    """Where the sampling of one turn stands.

    A turn is asked again after a reply cut off at max_tokens (once, at the higher cap) and
    after a pause_turn (up to MAX_PAUSES times).
    """

    retried: bool = False
    pauses: int = 0


class Move(StrEnum):
    USE = "use"  # the reply is complete: store it and act on it
    RETRY = "retry"  # cut off at max_tokens: ask again once, at the higher cap
    CONTINUE = "continue"  # pause_turn: ask again, with the text so far
    END = "end"  # the run ends with `outcome`


@dataclass(frozen=True)
class Judgement:
    move: Move
    sampling: Sampling  # the state to carry into the next call
    outcome: Outcome | None = None  # set exactly when the move is END


def judge(sampling: Sampling, reply: StopView) -> Judgement:
    """What to do with a reply. The stop reason decides, before any content (D9)."""
    match reply.stop_reason:
        case "end_turn" | "tool_use":
            return Judgement(Move.USE, sampling)
        case "max_tokens":
            if sampling.retried:
                return Judgement(Move.END, sampling, error(OUTPUT_LIMIT))
            return Judgement(Move.RETRY, replace(sampling, retried=True))
        case "pause_turn":
            pauses = sampling.pauses + 1
            if pauses > MAX_PAUSES:
                return Judgement(Move.END, sampling, error(PAUSE_LIMIT))
            return Judgement(Move.CONTINUE, replace(sampling, pauses=pauses))
        case "refusal":
            return Judgement(Move.END, sampling, error(declined(reply.stop_details)))
        case "model_context_window_exceeded":
            return Judgement(Move.END, sampling, error(CONTEXT_LIMIT))
        case "stop_sequence" | "other":
            reason = reply.raw_stop_reason or reply.stop_reason
            return Judgement(Move.END, sampling, error(f"unexpected stop reason: {reason}"))
        case _:
            assert_never(reply.stop_reason)


def is_complete(reply: StopView) -> bool:
    """True for a reply that is stored: judge would use it whatever the sampling state is."""
    return reply.stop_reason in ("end_turn", "tool_use")


def runnable_calls[T](reply: StopView, calls: Sequence[T]) -> list[T]:
    """The calls to run. Only a tool_use stop leaves any.

    On an end_turn reply a tool_use block would never get a result, and no provider should send
    one. If a mapping mistake in one does, the call still does not run.
    """
    return list(calls) if reply.stop_reason == "tool_use" else []


def carried_text(reply: LLMResponse) -> list[Block]:
    """The text of a paused reply, which goes back with the next request.

    Only text carries over a pause. A tool_use in a paused reply is never answered, so it must
    not reach a request or the transcript.
    """
    return [b for b in reply.content if isinstance(b, TextBlock) and b.text.strip()]


def join_pauses(reply: LLMResponse, paused: Sequence[Block]) -> LLMResponse:
    """The reply as stored: the text from before the pauses, then the reply itself."""
    if not paused:
        return reply
    return reply.model_copy(update={"content": [*paused, *reply.content]})


# ---------- a turn without tool calls ----------
@dataclass(frozen=True)
class Idle:
    count: int  # turns in a row without a tool call, this one included
    outcome: Outcome | None  # set when this turn ends the run; otherwise the caller sends NUDGE


def idle_turn(idle_before: int) -> Idle:
    count = idle_before + 1
    return Idle(count, error(NO_TOOL_CALLS) if count >= IDLE_LIMIT else None)


# ---------- the calls of a turn ----------
class CallLike(Protocol):
    """A tool call as either loop holds it: a ToolCall, or the workflow's ToolCallSummary.

    `input` is None when the workflow holds the arguments by reference (over 16 KB).
    """

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...

    @property
    def input(self) -> Mapping[str, Any] | None: ...


def finish_summary(arguments: Mapping[str, Any] | None) -> str | None:
    """The text the model gave to `finish`, or None when it gave no usable text."""
    if arguments is None:
        return None
    summary = arguments.get("summary")
    if not isinstance(summary, str):
        return None
    return summary.strip() or None


@dataclass(frozen=True)
class CallRun:
    results: list[ToolResult]  # one for each call that was run or answered, in call order
    finish: str | None  # the summary of a good finish, which ends the task


async def run_calls[C: CallLike](
    calls: Sequence[C], run: Callable[[C], Awaitable[ToolResult]]
) -> CallRun:
    """Run the calls one after another, in order, and stop at a good finish.

    `run` makes the call: the tool registry in-process, an activity in the workflow. Nothing
    runs for `finish`. A good finish ends the turn and the calls after it are skipped. A bad
    one, with no usable summary, is answered with an error result like any bad call, and the
    task goes on. An error from `run` is not caught: it is not the model's doing.
    """
    results: list[ToolResult] = []
    for call in calls:
        if call.name != FINISH:
            results.append(await run(call))
            continue
        summary = finish_summary(call.input)
        if summary is not None:
            return CallRun(results, summary)
        results.append(ToolResult(call_id=call.id, ok=False, output=BAD_FINISH))
    return CallRun(results, None)
