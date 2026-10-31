"""The agent loop with nothing around it: no Temporal, no blob store, no retries.

`run_in_process` calls the turn functions in order, so the durable workflow of Phase 2 can run
the same steps as activities. It stays afterwards as the test harness and as the baseline for
the durability overhead measure.

How a run ends. Finish is the one good end. Every other end is an ERROR outcome with a fixed
summary, so a caller can tell them apart without reading prose:

- the model calls `finish` with a summary: PR_OPENED with that summary and no pr_url, since
  no pull request exists in-process;
- `max_turns` turns pass: "turn limit";
- two turns in a row end without a tool call: "no tool calls". After the first the loop sends
  one nudge;
- a turn stops on max_tokens, is retried once at `max_tokens_retry`, and stops on it again:
  "output limit";
- the model refuses: "model declined (<category>): <explanation>";
- the context window fills, or the provider says the prompt is over it: "context limit".
  Compaction arrives in Phase 4;
- a turn is paused more than MAX_PAUSES times: "pause limit";
- a stop reason with no rule (stop_sequence, other): "unexpected stop reason: <reason>".

Stop reasons (D8, D9). The loop reads `stop_reason` before it reads any content. A reply that
stopped on max_tokens, a refusal or a full window is never stored and none of its tool calls
run: a tool_use cut off at max_tokens can be a valid but partial object, and a refusal may
hold output the model withdrew. Tool calls run only after a tool_use stop. A paused turn is
continued: the text so far goes back as the last message of the next request, and the stored
transcript gets one assistant message for the whole turn. Longhaul declares no server-side
tools, so the API should never send pause_turn. The rule is there so that one does not end the
run or run a half-finished reply. It has not met the live API: a request that ends on
assistant text is a prefill, which ADR-0003 lists as a 400 on Sonnet 5.5, and a 400 would
reach the caller as a ProviderError.

Errors that are not about the model's output are not caught. A ProviderError other than
CONTEXT_TOO_LONG goes up to the caller, because retries belong to Temporal and an in-process
run has no policy for them. The same goes for sandbox errors.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from longhaul.agent.prompts import FIRST_USER_MESSAGE, build_system_prompt
from longhaul.agent.turns import (
    append_assistant,
    append_tool_results,
    append_user_text,
    llm_step,
    tool_step,
    validate_pairs,
)
from longhaul.contracts import (
    Block,
    Budgets,
    CallOptions,
    EffortLevel,
    LLMProvider,
    LLMResponse,
    Message,
    Outcome,
    OutcomeKind,
    ProviderError,
    ProviderErrorKind,
    Role,
    Sandbox,
    StopDetails,
    TaskSpec,
    TextBlock,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultBlock,
)
from longhaul.settings import AppConfig
from longhaul.tools.control import FINISH
from longhaul.tools.registry import ToolRegistry

NUDGE = "Use tools or call finish."
MAX_PAUSES = 5
SUMMARY_WIDTH = 60

TURN_LIMIT = "turn limit"
OUTPUT_LIMIT = "output limit"
CONTEXT_LIMIT = "context limit"
PAUSE_LIMIT = "pause limit"
NO_TOOL_CALLS = "no tool calls"

BAD_FINISH = (
    "finish needs a summary: a short text that says what you changed. Call finish again with one."
)
SKIPPED_AFTER_FINISH = "finish was called earlier in this turn"

# The argument that says most about a call, in the order they are tried.
_SUMMARY_KEYS = ("path", "command", "pattern", "target", "summary")


# ---------- configuration ----------
class LoopConfig(BaseModel):
    """What one run needs to know. Frozen, so a running loop cannot be reconfigured.

    The defaults are the values in config/longhaul.example.yaml. `from_config` reads them from
    a loaded config instead. `role` picks the system prompt and labels the events, and it is a
    field because `run_in_process` takes no role argument of its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(min_length=1)
    max_turns: int = Field(default=Budgets().task_turns, ge=1)
    max_tokens_per_turn: int = Field(default=8192, ge=1)
    # The cap for the one retry of a turn that stopped on max_tokens (D8).
    max_tokens_retry: int = Field(default=64000, ge=1)
    effort: EffortLevel | None = None
    tool_output_max_tokens: int = Field(default=4000, ge=1)
    role: Role = Role.CODER

    @model_validator(mode="after")
    def _retry_cap_is_not_below_the_first_cap(self) -> Self:
        # The same rule as the loop section of the config.
        if self.max_tokens_retry < self.max_tokens_per_turn:
            raise ValueError(
                f"max_tokens_retry ({self.max_tokens_retry}) must not be below "
                f"max_tokens_per_turn ({self.max_tokens_per_turn}), or the retry could not "
                "finish a reply that the first try cut off"
            )
        return self

    @classmethod
    def from_config(cls, cfg: AppConfig, role: Role, *, max_turns: int | None = None) -> Self:
        """The settings for `role` from a loaded config.

        The turn limit is a budget of the task (RepoSettings.budgets.task_turns), which the
        config does not hold, so the caller passes it. Without it the contracts default is used.
        """
        # ModelIds and EffortLevels have one field per Role value.
        fields: dict[str, object] = {
            "model": getattr(cfg.models, role.value),
            "effort": getattr(cfg.effort, role.value),
            "max_tokens_per_turn": cfg.loop.max_tokens_per_turn,
            "max_tokens_retry": cfg.loop.max_tokens_retry,
            "tool_output_max_tokens": cfg.context.tool_output_max_tokens,
            "role": role,
        }
        if max_turns is not None:
            fields["max_turns"] = max_turns
        return cls.model_validate(fields)


# ---------- events and results ----------
@dataclass(frozen=True)
class CallEvent:
    name: str
    args: str  # the most telling argument, on one line and cut short
    ok: bool


@dataclass(frozen=True)
class TurnEvent:
    """One finished turn, for printing. `turn_index` starts at 0, like ToolContext.turn_index."""

    turn_index: int
    role: Role
    calls: tuple[CallEvent, ...]
    note: str | None = None  # a retry, a pause, a nudge


@dataclass(frozen=True)
class LoopResult:
    outcome: Outcome
    transcript: list[Message]
    turns: int  # model turns used. A retried or continued turn counts once.


EventSink = Callable[[TurnEvent], None]


# ---------- entry points ----------
async def run_in_process(
    spec: TaskSpec,
    provider: LLMProvider,
    registry: ToolRegistry,
    sandbox: Sandbox,
    cfg: LoopConfig,
    on_event: EventSink | None = None,
) -> Outcome:
    """Run the task until the loop ends. `on_event` is called once per finished turn."""
    return (await run_loop(spec, provider, registry, sandbox, cfg, on_event)).outcome


async def run_loop(
    spec: TaskSpec,
    provider: LLMProvider,
    registry: ToolRegistry,
    sandbox: Sandbox,
    cfg: LoopConfig,
    on_event: EventSink | None = None,
) -> LoopResult:
    """`run_in_process`, with the final transcript and the turn count next to the outcome.

    Tests and reports use the transcript. The durable layer needs the outcome only.
    """
    return await _Loop(spec, provider, registry, sandbox, cfg, on_event).run()


# ---------- the loop ----------
@dataclass(frozen=True)
class _Reply:
    resp: LLMResponse
    notes: list[str]


class _Loop:
    def __init__(
        self,
        spec: TaskSpec,
        provider: LLMProvider,
        registry: ToolRegistry,
        sandbox: Sandbox,
        cfg: LoopConfig,
        on_event: EventSink | None,
    ) -> None:
        self.spec, self.provider, self.registry = spec, provider, registry
        self.sandbox, self.cfg, self.on_event = sandbox, cfg, on_event
        # Built once. The system prompt, the tool list and the effort stay as they are for the
        # life of a transcript, because a change to any of them drops the prompt cache (D7, D8).
        self.system = build_system_prompt(cfg.role, spec)
        self.tools = registry.specs()
        self.options = CallOptions(effort=cfg.effort) if cfg.effort is not None else None
        self.transcript = append_user_text([], FIRST_USER_MESSAGE)
        self.idle = 0  # turns in a row that ended without a tool call

    async def run(self) -> LoopResult:
        for turn in range(self.cfg.max_turns):
            outcome = await self._turn(turn)
            if outcome is not None:
                return LoopResult(outcome, self.transcript, turn + 1)
        return LoopResult(_error(TURN_LIMIT), self.transcript, self.cfg.max_turns)

    async def _turn(self, turn: int) -> Outcome | None:
        # A transcript the API would reject fails here, with a message, and not as a 400.
        validate_pairs(self.transcript)
        sampled = await self._sample()
        if isinstance(sampled, Outcome):
            return sampled
        self.transcript = append_assistant(self.transcript, sampled.resp)
        calls = sampled.resp.tool_calls if sampled.resp.stop_reason == "tool_use" else []
        if not calls:
            return self._idle_turn(turn, sampled.notes)
        self.idle = 0
        return await self._tool_turn(turn, calls, sampled.notes)

    # ----- one model reply -----
    async def _sample(self) -> _Reply | Outcome:
        """Call the model until one reply can be used, or the run has to end."""
        cap, retried, pauses = self.cfg.max_tokens_per_turn, False, 0
        paused: list[Block] = []
        while True:
            resp = await self._complete(paused, cap)
            if isinstance(resp, Outcome):
                return resp
            ended = _ending(resp)
            if ended is not None:
                return ended
            if resp.stop_reason == "max_tokens":
                if retried:
                    return _error(OUTPUT_LIMIT)
                retried, cap = True, self.cfg.max_tokens_retry
            elif resp.stop_reason == "pause_turn":
                pauses += 1
                if pauses > MAX_PAUSES:
                    return _error(PAUSE_LIMIT)
                paused.extend(_text_of(resp))
            else:
                return _Reply(_after_pauses(resp, paused), self._sampling_notes(retried, pauses))

    async def _complete(self, paused: list[Block], cap: int) -> LLMResponse | Outcome:
        request = self.transcript
        if paused:
            request = [*request, Message(role="assistant", content=paused)]
        try:
            return await llm_step(
                self.provider, self.cfg.model, self.system, request, self.tools, cap, self.options
            )
        except ProviderError as exc:
            if exc.kind is ProviderErrorKind.CONTEXT_TOO_LONG:
                return _error(CONTEXT_LIMIT)
            raise

    def _sampling_notes(self, retried: bool, pauses: int) -> list[str]:
        notes = []
        if retried:
            notes.append(f"retried at max_tokens {self.cfg.max_tokens_retry}")
        if pauses:
            notes.append(f"continued after pause_turn x{pauses}")
        return notes

    # ----- what a reply leads to -----
    def _idle_turn(self, turn: int, notes: list[str]) -> Outcome | None:
        self.idle += 1
        if self.idle >= 2:
            self._emit(turn, [], [*notes, "no tool calls twice in a row; stopping"])
            return _error(NO_TOOL_CALLS)
        self.transcript = append_user_text(self.transcript, NUDGE)
        self._emit(turn, [], [*notes, "no tool calls; nudged"])
        return None

    async def _tool_turn(
        self, turn: int, calls: list[ToolCall], notes: list[str]
    ) -> Outcome | None:
        ctx = ToolContext(
            task_id=self.spec.task_id,
            turn_index=turn,
            blob_prefix=f"tasks/{self.spec.task_id}",
            output_max_tokens=self.cfg.tool_output_max_tokens,
        )
        results: list[ToolResult] = []
        summary: str | None = None
        for call in calls:
            if call.name == FINISH:
                # The loop handles finish itself and nothing runs for it. A good finish ends the
                # task and the calls after it are skipped. A bad one is answered like any bad call.
                summary = _finish_summary(call)
                if summary is not None:
                    break
                results.append(ToolResult(call_id=call.id, ok=False, output=BAD_FINISH))
            else:
                results.append(await tool_step(self.registry, self.sandbox, call, ctx))
        self.transcript = append_tool_results(
            self.transcript, calls, results, not_run_reason=SKIPPED_AFTER_FINISH
        )
        self._emit(turn, calls, notes)
        if summary is None:
            return None
        return Outcome(kind=OutcomeKind.PR_OPENED, summary=summary)

    def _emit(self, turn: int, calls: list[ToolCall], notes: list[str]) -> None:
        if self.on_event is None:
            return
        # The results just written are the one record of what each call came to, finish and
        # the calls that were skipped included.
        last = self.transcript[-1].content if calls else []
        ok = {b.tool_use_id: not b.is_error for b in last if isinstance(b, ToolResultBlock)}
        shown = tuple(CallEvent(c.name, _short_args(c), ok.get(c.id, False)) for c in calls)
        self.on_event(TurnEvent(turn, self.cfg.role, shown, "; ".join(notes) or None))


# ---------- helpers ----------
def _error(summary: str) -> Outcome:
    return Outcome(kind=OutcomeKind.ERROR, summary=summary)


def _ending(resp: LLMResponse) -> Outcome | None:
    """The outcome for a stop reason that ends the run, or None when the reply goes on."""
    match resp.stop_reason:
        case "refusal":
            return _error(_declined(resp.stop_details))
        case "model_context_window_exceeded":
            return _error(CONTEXT_LIMIT)
        case "stop_sequence" | "other":
            return _error(f"unexpected stop reason: {resp.raw_stop_reason or resp.stop_reason}")
        case _:
            return None


def _declined(details: StopDetails | None) -> str:
    # The API can send a refusal with no details at all, or with one of the two missing.
    category = (details.category or "").strip() if details is not None else ""
    explanation = (details.explanation or "").strip() if details is not None else ""
    return f"model declined ({category or 'unknown'}): {explanation or 'no explanation given'}"


def _text_of(resp: LLMResponse) -> list[Block]:
    # Only text carries over a pause. A tool_use in a paused reply is never answered, so it
    # must not reach a request or the transcript.
    return [b for b in resp.content if isinstance(b, TextBlock) and b.text.strip()]


def _after_pauses(resp: LLMResponse, paused: list[Block]) -> LLMResponse:
    if not paused:
        return resp
    return resp.model_copy(update={"content": [*paused, *resp.content]})


def _finish_summary(call: ToolCall) -> str | None:
    summary = call.input.get("summary")
    if not isinstance(summary, str):
        return None
    return summary.strip() or None


def _short_args(call: ToolCall) -> str:
    for key in _SUMMARY_KEYS:
        value = call.input.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value)
    return ""


def _clip(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= SUMMARY_WIDTH:
        return one_line
    return one_line[: SUMMARY_WIDTH - 3] + "..."
