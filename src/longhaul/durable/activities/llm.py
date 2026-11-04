"""The llm_turn activity: one model call for one turn, with its blobs, summaries and row.

The workflow keeps the transcript in the blob store (ADR-0005) and calls this activity by name.
The activity loads the transcript, builds the request the way the in-process loop does, makes
the one call, stores what came back and returns refs and small summaries. What to do with the
reply is the workflow's decision, made with the rules in longhaul.agent.rules.

A reply is added to the transcript only when it is complete (end_turn or tool_use). A reply cut
off at max_tokens, a refusal, a full window or a pause is not stored, and none of its tool calls
are returned. For a turn that was paused, the text so far is stored under `paused_ref` and the
workflow hands it back with the next call.

Every call that was billed has a row in `turns`, also when it failed. A call that failed before
it was billed leaves no row.

Every call that was billed is also on the cost ledger (invariant I6), with the Temporal attempt
number. It is written as soon as the call returns, before the blobs and the `turns` row, so a
call that is billed and then lost to a failure of this activity is still on the ledger. Temporal
runs the activity again and the model is called again, which makes a second row with attempt 2.
The cost on the ledger is the cost in `LlmTurnOutput` and in the `turns` row.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic_core import to_json
from temporalio import activity

from longhaul.agent import rules
from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import FIRST_USER_MESSAGE, build_system_prompt
from longhaul.agent.turns import (
    TranscriptError,
    append_assistant,
    append_user_text,
    llm_step,
    validate_pairs,
)
from longhaul.blobs.base import BlobStoreBase, blob_key
from longhaul.contracts import (
    BlobRef,
    Block,
    CallOptions,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    Role,
    TextBlock,
    ToolCall,
    ToolSpec,
    Usage,
)
from longhaul.cost.ledger import Ledger, NoLedger
from longhaul.cost.pricing import PricingError
from longhaul.db.records import Recorder, TurnRecord
from longhaul.durable import acts
from longhaul.durable.activities.support import (
    call_slug,
    heartbeating,
    not_retried,
    provider_failure,
    small,
)
from longhaul.durable.payload_guard import json_size
from longhaul.telemetry.otel import chat_span


class LlmActivities:
    """The llm_turn activity and what it needs.

    `config_for` gives the model, the effort and the output caps of a role, which the workflow
    cannot read. `tools` is the tool list sent with every call: it stays as it is for the life
    of a transcript, because a change drops the prompt cache (D7, D8).

    `ledger` writes the cost ledger row of every call. A worker passes a `SqlLedger` built on
    the prices of `load_pricing`. Without one no row is written and every call costs 0, which
    suits a run with no database and no paid provider, and nothing else.
    """

    def __init__(
        self,
        provider: LLMProvider,
        blobs: BlobStoreBase,
        recorder: Recorder,
        tools: Sequence[ToolSpec],
        config_for: Callable[[Role], LoopConfig],
        *,
        ledger: Ledger | None = None,
    ) -> None:
        self._provider = provider
        self._blobs = blobs
        self._recorder = recorder
        self._tools = list(tools)
        self._config_for = config_for
        self._ledger: Ledger = NoLedger() if ledger is None else ledger

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.llm_turn]

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        inp = small(inp)
        cfg = self._config_for(inp.role)
        try:
            system = build_system_prompt(inp.role, inp.spec)
            transcript = await self._transcript(inp)
            validate_pairs(transcript)  # a transcript the API would reject is not sent
        except ValueError as exc:  # also TranscriptError and a damaged blob
            raise not_retried(exc) from exc
        paused = await self._paused(inp.paused_ref)
        attempt = await self._recorder.next_attempt(inp.task_id, inp.turn_index)
        started = datetime.now(UTC)
        began = time.monotonic()
        resp, cost = await self._ask(inp, cfg, system, transcript, paused, attempt, started)
        latency_ms = resp.latency_ms or int((time.monotonic() - began) * 1000)
        out = await self._store(inp, resp, transcript, paused, attempt, cost)
        await self._recorder.record_turn(
            TurnRecord(
                task_id=inp.task_id,
                turn_index=inp.turn_index,
                attempt=attempt,
                role=inp.role.value,
                started_at=started,
                request_ref=inp.transcript_ref.key if inp.transcript_ref else None,
                response_ref=out.response_ref.key if out.response_ref else None,
                usage=resp.usage,
                cost_usd=out.cost_usd,
                stop_reason=resp.stop_reason,
                latency_ms=latency_ms,
                ended_at=datetime.now(UTC),
            )
        )
        return small(out)

    # ----- the request -----
    async def _transcript(self, inp: acts.LlmTurnInput) -> list[Message]:
        if inp.transcript_ref is None:
            return append_user_text([], FIRST_USER_MESSAGE)
        return await self._blobs.get_json(inp.transcript_ref, list[Message])

    async def _paused(self, ref: BlobRef | None) -> list[Block]:
        if ref is None:
            return []
        return [*await self._blobs.get_json(ref, list[TextBlock])]

    # ----- the call -----
    async def _ask(
        self,
        inp: acts.LlmTurnInput,
        cfg: LoopConfig,
        system: str,
        transcript: list[Message],
        paused: list[Block],
        attempt: int,
        started: datetime,
    ) -> tuple[LLMResponse, Decimal]:
        """Call the model once and put the call on the ledger. Returns the reply and its cost.

        The ledger row is written inside the chat span, as soon as the call returns, so the span
        carries the same price as the row.
        """
        # The text of a paused reply goes back as the last message, which the API continues.
        request = [*transcript, Message(role="assistant", content=paused)] if paused else transcript
        cap = cfg.max_tokens_retry if inp.high_output_cap else cfg.max_tokens_per_turn
        options = CallOptions(effort=cfg.effort) if cfg.effort is not None else None
        try:
            with chat_span(
                model=cfg.model,
                provider=self._provider.name,
                task_id=inp.task_id,
                turn_index=inp.turn_index,
                role=inp.role,
                system=system,
                messages=request,
            ) as span:
                async with heartbeating():
                    resp = await llm_step(
                        self._provider, cfg.model, system, request, self._tools, cap, options
                    )
                cost = await self._charge(inp, cfg.model, resp.usage)
                span.record(resp, cost_usd=float(cost))
            return resp, cost
        except ProviderError as exc:
            if exc.usage is not None:
                # Billed, then failed: the call is on the ledger and in `turns` before the error
                # goes up.
                cost = await self._charge(inp, cfg.model, exc.usage)
                await self._recorder.record_turn(
                    TurnRecord(
                        task_id=inp.task_id,
                        turn_index=inp.turn_index,
                        attempt=attempt,
                        role=inp.role.value,
                        started_at=started,
                        request_ref=inp.transcript_ref.key if inp.transcript_ref else None,
                        usage=exc.usage,
                        cost_usd=float(cost),
                        ended_at=datetime.now(UTC),
                    )
                )
            raise provider_failure(exc) from exc

    async def _charge(self, inp: acts.LlmTurnInput, model: str, usage: Usage) -> Decimal:
        """Put a call on the ledger and return what it cost.

        This is the first thing done with a reply, so that the call is on the ledger even when
        the activity fails after it. The row carries the Temporal attempt, so a retry shows as
        attempt 2 (plan section 4.1, F1). `turns` counts the calls of a turn instead. The model
        is the one that was asked for, because that is the name the pricing file knows.

        A model without a price fails for good. Each retry would call the model again, and none
        of those calls could be priced.
        """
        try:
            return await self._ledger.record(
                inp.task_id, inp.spec.repo, model, inp.turn_index, activity.info().attempt, usage
            )
        except PricingError as exc:
            raise not_retried(exc) from exc

    # ----- what came back -----
    async def _store(
        self,
        inp: acts.LlmTurnInput,
        resp: LLMResponse,
        transcript: list[Message],
        paused: list[Block],
        attempt: int,
        cost: Decimal,
    ) -> acts.LlmTurnOutput:
        complete = rules.is_complete(resp)
        grown = self._grow(transcript, rules.join_pauses(resp, paused)) if complete else transcript
        calls = rules.runnable_calls(resp, resp.tool_calls)

        def key(name: str) -> str:
            return blob_key(inp.task_id, "turns", inp.turn_index, name)

        response_ref = await self._blobs.put_json(resp, key(f"response-{attempt}.json"))
        if len(grown) == len(transcript) and inp.transcript_ref is not None:
            transcript_ref = inp.transcript_ref  # nothing was added, so there is nothing to store
        else:
            transcript_ref = await self._blobs.put_json(grown, key(f"transcript-{attempt}.json"))
        tool_calls_ref = None
        if calls:
            tool_calls_ref = await self._blobs.put_json(calls, key(f"tool_calls-{attempt}.json"))
        carried = [*paused, *rules.carried_text(resp)] if resp.stop_reason == "pause_turn" else []
        paused_ref = None
        if carried:
            paused_ref = await self._blobs.put_json(carried, key(f"paused-{attempt}.json"))
        usage = resp.usage
        return acts.LlmTurnOutput(
            transcript_ref=transcript_ref,
            tool_calls_ref=tool_calls_ref,
            tool_call_summaries=await self._summaries(inp, calls),
            stop_reason=resp.stop_reason,
            usage=usage,
            cost_usd=float(cost),
            input_tokens_total=usage.input_tokens
            + usage.cache_read_tokens
            + usage.cache_write_tokens,
            stop_details=resp.stop_details,
            raw_stop_reason=resp.raw_stop_reason,
            response_ref=response_ref,
            paused_ref=paused_ref,
        )

    @staticmethod
    def _grow(transcript: list[Message], reply: LLMResponse) -> list[Message]:
        try:
            return append_assistant(transcript, reply)
        except TranscriptError as exc:
            raise not_retried(exc) from exc

    async def _summaries(
        self, inp: acts.LlmTurnInput, calls: list[ToolCall]
    ) -> list[acts.ToolCallSummary]:
        """The calls as the workflow sees them: arguments inline while they are small.

        One call may go inline up to INLINE_LIMIT_BYTES, and one reply up to
        INLINE_ARGS_TOTAL_BYTES in all. The rest are stored and named by ref.
        """
        summaries: list[acts.ToolCallSummary] = []
        inline_total = 0
        for call in calls:
            size = json_size(call.input)
            if (
                size <= acts.INLINE_LIMIT_BYTES
                and inline_total + size <= acts.INLINE_ARGS_TOTAL_BYTES
            ):
                inline_total += size
                summaries.append(acts.ToolCallSummary(id=call.id, name=call.name, input=call.input))
                continue
            name = f"args-{call_slug(call.id)}.json"
            key = blob_key(inp.task_id, "turns", inp.turn_index, name)
            ref = await self._blobs.put(to_json(call.input), key=key)
            summaries.append(acts.ToolCallSummary(id=call.id, name=call.name, args_ref=ref))
        return summaries
