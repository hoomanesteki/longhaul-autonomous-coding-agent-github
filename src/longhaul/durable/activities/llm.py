"""The llm_turn activity: one model call for one turn, with its blobs, summaries and row.

The workflow keeps the transcript in the blob store (ADR-0005) and calls this activity by name.
The activity loads the transcript, builds the request the way the in-process loop does, makes
the one call, stores what came back and returns refs and small summaries. What to do with the
reply is the workflow's decision, made with the rules in longhaul.agent.rules.

A reply is added to the transcript only when it is complete (end_turn or tool_use). A reply cut
off at max_tokens, a refusal, a full window or a pause is not stored, and none of its tool calls
are returned. For a turn that was paused, the text so far is stored under `paused_ref` and the
workflow hands it back with the next call.

Hints. The workflow passes every hint of the task with every call, and this activity shows the
model the ones it has not read: `show_hints` adds each as user text to the last message, once,
and the transcript is the record of it. The text goes into the stored transcript together with
a complete reply. A reply that is not stored takes the hint with it, and the call that asks
again adds it again, so the model reads it once and the transcript holds it once. The system
prompt and the tool list do not change (D7), and nothing before the last message changes, so the
cached prefix stays valid. The `request_ref` of a turn row names the transcript the call began
from, without the hints it added, as it does not hold the text of a paused reply either.

Every call that was billed has a row in `turns`, also when it failed. A call that failed before
it was billed leaves no row.

Every call that was billed is also on the cost ledger (invariant I6), with the Temporal attempt
number. It is written as soon as the call returns, before the blobs and the `turns` row, so a
call that is billed and then lost to a failure of this activity is still on the ledger. Temporal
runs the activity again and the model is called again, which makes a second row with attempt 2.
The cost on the ledger is the cost in `LlmTurnOutput` and in the `turns` row.

Metrics (LH-704). A call that returns a reply counts as a turn and adds its cache ratio. A call
that fails counts as an error of its kind. The cost counter follows the ledger: it grows by the
price that `_charge` got back, for a call that failed after it was billed as well.

Who answers. A task that carries a script (`script_ref`, from `longhaul run --script`) is answered
by a scripted provider built from it, and any other task by the provider of the worker. A worker
whose provider is the scripted one but that was given no script has none of its own, and refuses
a task without a script for good, since asking again changes nothing (support.provider_for_task).

The two views of a transcript (LH-404). The transcript the workflow carries (`transcript_ref`) is
the view: what the model sees. Before each call this activity applies tier 1 of the context
strategy to it, so that tool results older than `context.elide_tool_outputs_after_turns` turns
are stubs with a handle (context/evict.py). The reply is appended to the elided view, and that is
what is stored. The log is the other record, the whole transcript with nothing elided, and the
activity adds to it as well (context/log.py): the hints it showed and the reply, as the `reply`
part of the turn. `append_results` writes the `results` part. The log keeps every turn and the
view keeps what fits.

Tier 2 is decided here, because only here can the tokens be counted (the workflow cannot, and the
transcript is a blob). A meter that learns from the replies of the task (support.Meters) projects
the request before it is sent. A request over `hard_limit` of the window is not sent: the
activity fails with CONTEXT_TOO_LONG, as the provider would, and the workflow compacts down to the
last two turns and asks once more (F6). Close to the thresholds the provider is asked for an exact
count. After the reply the activity projects the request after this one, with one more result
elided and room for the results of the calls it just made (each up to `tool_output_max_tokens`),
and returns what to do before then in `context_decision`: go on, compact, or force. The workflow
runs the compaction when this turn is over.

The memory block (LH-604). `LlmTurnInput.memory_ref` names the blob that load_memory wrote once for
the task, and its text ends the system prompt (block 4). The workflow passes the same ref on every
turn of every run, so the prompt is the same bytes throughout and the cache of the system prompt
and the tool list stays valid. A blob that cannot be read is retried like the transcript.

The notes. After a compaction the notes are in the first message of the view, right after the task
text, where context/compact.py `apply` put them. The system prompt does not change (D7), so the
cached prefix survives a compaction as far as its first message. `LlmTurnInput.notes` is the
workflow's own copy and is not used to build the request.

The modes (LH-406). `context.mode` switches the tiers off for an ablation, and a run can name its
own mode, which travels in `LlmTurnInput.context_mode` and wins over the config
(longhaul.context.modes). Tier 1 runs in `full` and `no_compaction`. The decision is always "ok"
unless the mode is `full`, so a workflow that acts on the decision never compacts in the others.
The guard in front of the call stays on in every mode: it is how a task in `none` ends, at the hard
limit, with the outcome "context limit" and not with a provider error. The mode `sliding` (LH-903,
the naive baseline) answers the guard another way: before the request is measured against the limit,
the oldest whole turns are dropped until it fits (context/sliding.py), and the cut view is what is
sent and stored. The guard then refuses only a request that no cut can bring under the limit.

The share of the window. A request that passes the guard is going out, and the activity notes what
share of the window it takes (`Recorder.record_request_fraction`), so that `tasks.max_fraction`
holds the largest. That is the projection, which is the number the guard works with. A refused
request is not noted, because it is not sent.

Without a `context` policy the activity elides nothing, projects nothing and always says "ok".
The log is written either way.

Roles (LH-501). Each role has its own system prompt, first message and tool list. The prompt and
the first message come from agent/prompts.py, and the tool list from `role_tools`, which maps a
role to the tools it is offered. A role without an entry gets `tools`, so a worker that was built
before the planner existed keeps working. The list of a role is as fixed as the prompt for the
life of its transcript (D7).

A window of the run's own. `longhaul run --window` puts a window in the tuning of the task, and the
workflow hands it to each call in `LlmTurnInput.window_tokens`. The call then uses the policy of
the config with that window and with the cap for one result cut to fit it
(settings.Context.for_window), for the guard, for the room kept for results and for the decision.
The other thresholds are shares of the window, so they follow it.

The coder of a task with a plan (LH-503) is the one role with two forms. A plan in the input
(`LlmTurnInput.plan`) makes the coder's prompt `build_planned_system_prompt`, its first message the
one that points at the plan, and its tool list `planned_tools`, which has `update_step`,
`submit_for_review` and `ask_human` in place of `finish`. A task has a plan from before the coder's
transcript starts or not at all, so each of the three is the same for the life of the transcript.
The plan is the fifth block of the first message (agent/prompts.py), and the activity brings that
block up to date before every call from the plan the workflow holds, which is how a step update
reaches the model. A worker with no `planned_tools` offers such a coder `tools`, as it did before.

The verifier (LH-504) has a transcript of its own, and its first message is built here, from what
the workflow gathered before the first turn. The workflow cannot write blobs, so it runs the
pre-check and the `git_diff` through tool_exec, which stores their full output, and hands over
`LlmTurnInput.review`: each command with how it ended and the ref of its output, and the plan. The
first message is then the line that starts the review, the plan block, the pre-check block and the
diff block (roles/verifier.py renders the last two). Each output is cut to a head, a tail and a
handle, so a long test log or diff costs the window what any tool output does, and the verifier
pages through the rest with `read_output`. This happens when the transcript starts only. A later
turn of the review reads the transcript, which holds that message already.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic_core import to_json
from temporalio import activity

from longhaul.agent import rules
from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import (
    build_planned_system_prompt,
    build_system_prompt,
    first_user_message,
    render_plan,
    with_plan,
)
from longhaul.agent.turns import (
    TranscriptError,
    append_assistant,
    append_user_text,
    llm_step,
    show_hints,
    validate_pairs,
)
from longhaul.blobs.base import BlobStoreBase, blob_key
from longhaul.context import sliding
from longhaul.context.evict import elide_old_outputs
from longhaul.context.log import added_messages, append_to_log
from longhaul.context.modes import ContextMode
from longhaul.context.tokens import ContextBudget, Decision, TokenMeter, count_request_chars
from longhaul.context.truncate import shape_output
from longhaul.contracts import (
    BlobRef,
    Block,
    CallOptions,
    LLMProvider,
    LLMResponse,
    Message,
    ProviderError,
    ProviderErrorKind,
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
from longhaul.durable.activities.memory import read_memory_block
from longhaul.durable.activities.support import (
    Meters,
    call_slug,
    heartbeating,
    not_retried,
    provider_failure,
    provider_for_task,
    small,
)
from longhaul.durable.payload_guard import json_size
from longhaul.durable.scripts import TaskProviders
from longhaul.roles import coder, verifier
from longhaul.settings import Context
from longhaul.telemetry import metrics
from longhaul.telemetry.otel import chat_span
from longhaul.tools.control import is_control_tool
from longhaul.tools.output import output_handle


@dataclass(frozen=True)
class _View:
    """The transcript of one call at the points where llm_turn looks at it.

    `before` is the stored view, or nothing for the first turn. `shown` adds the hints that the
    call shows. `sent` is `shown` after tier 1, which is the request apart from the paused text.
    """

    before: list[Message]
    shown: list[Message]
    sent: list[Message]
    paused: list[Block]


def _budget(context: Context | None) -> ContextBudget | None:
    if context is None:
        return None
    if context.elide_tool_outputs_after_turns < 1:
        raise ValueError(
            "context.elide_tool_outputs_after_turns has to be at least 1, got "
            f"{context.elide_tool_outputs_after_turns}"
        )
    return ContextBudget(context.window_tokens, context.compact_at, context.hard_limit)


def _answered_by_the_workflow(tool: str) -> bool:
    """True for a call whose answer is a line from the workflow, so no output to reserve for."""
    return is_control_tool(tool) or tool in coder.CONTROL_TOOLS or tool == verifier.SUBMIT_VERDICT


def _options(cfg: LoopConfig) -> CallOptions | None:
    return CallOptions(effort=cfg.effort) if cfg.effort is not None else None


class LlmActivities:
    """The llm_turn activity and what it needs.

    `provider` answers the tasks that carry no script. It is None for a worker that has none, and
    then only tasks with a script can run. `config_for` gives the model, the effort and the
    output caps of a role, which the workflow cannot read. `tools` is the tool list sent with
    every call: it stays as it is for the life of a transcript, because a change drops the prompt
    cache (D7, D8).

    `ledger` writes the cost ledger row of every call. A worker passes a `SqlLedger` built on
    the prices of `load_pricing`. Without one no row is written and every call costs 0, which
    suits a run with no database and no paid provider, and nothing else.

    `context` is the policy of the two context tiers (config section `context`). Without it the
    activity runs as it did before LH-404: nothing is elided and no request is projected.
    `meters` holds the token meter of each task, and the compact activity of the same worker
    shares it, so both count with the ratio the replies taught.

    `role_tools` gives a role the tools it is offered when they are not `tools`. The planner has
    its own (roles/planner.py). `planned_tools` is the list of the coder of a task with a plan
    (roles/coder.py `tool_specs`).
    """

    def __init__(
        self,
        provider: LLMProvider | None,
        blobs: BlobStoreBase,
        recorder: Recorder,
        tools: Sequence[ToolSpec],
        config_for: Callable[[Role], LoopConfig],
        *,
        ledger: Ledger | None = None,
        context: Context | None = None,
        meters: Meters | None = None,
        role_tools: Mapping[Role, Sequence[ToolSpec]] | None = None,
        planned_tools: Sequence[ToolSpec] | None = None,
    ) -> None:
        self._provider = provider
        self._scripts = TaskProviders(blobs)
        self._blobs = blobs
        self._recorder = recorder
        self._tools = list(tools)
        self._role_tools = {role: list(specs) for role, specs in (role_tools or {}).items()}
        self._planned_tools = None if planned_tools is None else list(planned_tools)
        self._config_for = config_for
        self._ledger: Ledger = NoLedger() if ledger is None else ledger
        self._context = context
        self._budget = _budget(context)
        self._meters = meters if meters is not None else Meters()

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.llm_turn]

    def tools_for(self, role: Role, *, planned: bool = False) -> list[ToolSpec]:
        """The tools that `role` is offered with every call.

        `planned` is for the coder of a task that has a plan. No other role has a second list.
        """
        if planned and role is Role.CODER and self._planned_tools is not None:
            return self._planned_tools
        return self._role_tools.get(role, self._tools)

    @activity.defn(name=acts.LLM_TURN.name)
    async def llm_turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        # A run that asked for a window of its own is served by a copy of this activity with that
        # window in its policy, so that nothing below needs to know that a run can ask.
        return await self._scoped(inp.window_tokens)._turn(inp)

    def _scoped(self, window_tokens: int | None) -> LlmActivities:
        """This activity for a run with the window `window_tokens`, or itself when it has none.

        The copy shares everything but the policy and the budget: the stores, the ledger, the
        token meters and the provider. It is made for the call and dropped after it, so one run's
        window never reaches the next. Without a context policy there is nothing to scope.
        """
        if window_tokens is None or self._context is None:
            return self
        scoped = copy.copy(self)
        scoped._context = self._context.for_window(window_tokens)
        scoped._budget = _budget(scoped._context)
        return scoped

    async def _turn(self, inp: acts.LlmTurnInput) -> acts.LlmTurnOutput:
        inp = small(inp)
        cfg = self._config_for(inp.role)
        mode = self._mode(inp.context_mode)
        planned = inp.role is Role.CODER and inp.plan is not None
        tools = self.tools_for(inp.role, planned=planned)
        try:
            # The block is blob text that was written once for the task, so the system prompt is
            # the same bytes on every turn (D7). A task without one gets the prompt it always had.
            memory_block = await read_memory_block(self._blobs, inp.memory_ref)
            system = (
                build_planned_system_prompt(inp.spec, memory_block)
                if planned
                else build_system_prompt(inp.role, inp.spec, memory_block)
            )
            stored = await self._transcript(inp, planned=planned, cap=cfg.tool_output_max_tokens)
            if planned and inp.plan is not None:
                stored = with_plan(stored, inp.plan)  # a step update reaches the model here
            shown = show_hints(stored, inp.hints)
            transcript = self._evict(shown, mode)  # tier 1
            validate_pairs(transcript)  # a transcript the API would reject is not sent
        except ValueError as exc:  # also TranscriptError and a damaged blob
            raise not_retried(exc) from exc
        paused = await self._paused(inp.paused_ref)
        provider = await self._provider_for(inp)
        meter = self._meters.for_task(inp.task_id)
        measured: int | None = None
        if mode.slides:
            transcript, measured = await self._slide(
                provider, cfg, system, transcript, paused, tools, meter
            )
        request = self._with_paused(transcript, paused)
        projected = await self._preflight(provider, cfg, system, request, tools, meter, measured)
        await self._note_share(inp.task_id, projected)
        attempt = await self._recorder.next_attempt(inp.task_id, inp.turn_index)
        started = datetime.now(UTC)
        began = time.monotonic()
        resp, cost = await self._ask(provider, inp, cfg, system, request, tools, attempt, started)
        if self._context is not None:
            meter.observe(resp.usage, count_request_chars(system, request, tools))
        latency_ms = resp.latency_ms or int((time.monotonic() - began) * 1000)
        view = _View(
            before=stored if inp.transcript_ref is not None else [],
            shown=shown,
            sent=transcript,
            paused=paused,
        )
        out = await self._store(
            inp, resp, view, attempt, cost, system, tools, meter, projected, mode
        )
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
    async def _transcript(
        self, inp: acts.LlmTurnInput, *, planned: bool, cap: int
    ) -> list[Message]:
        if inp.transcript_ref is not None:
            return await self._blobs.get_json(inp.transcript_ref, list[Message])
        messages = append_user_text([], first_user_message(inp.role, planned=planned))
        if inp.role is Role.VERIFIER:
            for text in await self._review_blocks(inp, cap):
                messages = append_user_text(messages, text)
        return messages

    async def _review_blocks(self, inp: acts.LlmTurnInput, cap: int) -> list[str]:
        """The blocks after the first line of a review: the plan, the pre-check and the diff.

        The text of each output is read from its blob and cut by the rule of any tool output
        (context/truncate.py), with the handle `out_<call id>` that read_output resolves. The
        pre-check gets a smaller share than the diff, because the start of a test log has the
        first failure and its end has the counts.
        """
        blocks: list[str] = []
        if inp.plan is not None:
            blocks.append(render_plan(inp.plan))
        if inp.review is not None:
            shown = [
                (run, await self._shaped(run, verifier.CHECK_OUTPUT_TOKENS))
                for run in inp.review.checks
            ]
            blocks.append(verifier.render_precheck(shown))
            if inp.review.diff is not None:
                blocks.append(verifier.render_diff(await self._shaped(inp.review.diff, cap)))
        return blocks

    async def _shaped(self, run: acts.CheckRun, max_tokens: int) -> str:
        if run.output_ref is None:
            return ""
        text = (await self._blobs.get(run.output_ref)).decode("utf-8", errors="replace")
        return shape_output(text, max_tokens, output_handle(run.call_id))

    async def _paused(self, ref: BlobRef | None) -> list[Block]:
        if ref is None:
            return []
        return [*await self._blobs.get_json(ref, list[TextBlock])]

    async def _provider_for(self, inp: acts.LlmTurnInput) -> LLMProvider:
        """The script of the task if it has one, else the provider of the worker."""
        return await provider_for_task(self._scripts, self._provider, inp.task_id, inp.script_ref)

    def _mode(self, asked: ContextMode | None) -> ContextMode:
        """The mode of this call: the one the run asked for, else the one in the config."""
        if asked is not None:
            return asked
        return self._context.mode if self._context is not None else ContextMode.FULL

    def _evict(self, transcript: list[Message], mode: ContextMode) -> list[Message]:
        """Tier 1: the results older than the configured number of turns become stubs."""
        if self._context is None or not mode.elides:
            return transcript
        return elide_old_outputs(transcript, self._context.elide_tool_outputs_after_turns)

    async def _note_share(self, task_id: str, projected: int) -> None:
        """Note the share of the window that the request about to be sent takes."""
        if self._budget is not None:
            await self._recorder.record_request_fraction(task_id, projected / self._budget.window)

    @staticmethod
    def _with_paused(transcript: list[Message], paused: list[Block]) -> list[Message]:
        """The request: the view, then the text of a paused reply as the last message.

        The API continues that message, so it goes at the end and is not part of the view.
        """
        return [*transcript, Message(role="assistant", content=paused)] if paused else transcript

    # ----- tier 2: the size of the request -----
    async def _measure(
        self,
        provider: LLMProvider,
        cfg: LoopConfig,
        system: str,
        request: list[Message],
        tools: list[ToolSpec],
        meter: TokenMeter,
    ) -> int:
        """The projected tokens of the request, or 0 when there is no context policy.

        Close to the thresholds the estimate is replaced by the provider's exact count, if it
        gives one.
        """
        budget = self._budget
        if budget is None:
            return 0
        tokens = meter.projected(system, request, tools)
        if budget.needs_exact_count(tokens):
            tokens = await self._exact_count(provider, cfg, system, request, tools) or tokens
        return tokens

    async def _slide(
        self,
        provider: LLMProvider,
        cfg: LoopConfig,
        system: str,
        transcript: list[Message],
        paused: list[Block],
        tools: list[ToolSpec],
        meter: TokenMeter,
    ) -> tuple[list[Message], int | None]:
        """The sliding window (mode `sliding`): the newest turns that fit under the hard limit.

        The request is measured as it will be sent, with the system prompt, the tools of the role
        and the text of a paused reply, so what comes back passes the guard in `_preflight` unless
        no cut can make it fit. A request that is under the limit is returned as it is. The
        measure of the view that is returned goes with it, so that the guard does not ask the
        provider for the same count a second time.
        """
        budget = self._budget
        if budget is None:
            return transcript, None

        async def measure(view: list[Message]) -> int:
            request = self._with_paused(view, paused)
            return await self._measure(provider, cfg, system, request, tools, meter)

        def over(tokens: int) -> bool:
            return budget.decide(tokens) == "force"

        return await sliding.fit(transcript, measure, over)

    async def _preflight(
        self,
        provider: LLMProvider,
        cfg: LoopConfig,
        system: str,
        request: list[Message],
        tools: list[ToolSpec],
        meter: TokenMeter,
        measured: int | None = None,
    ) -> int:
        """The projected tokens of the request, or CONTEXT_TOO_LONG when it is over the limit.

        A request at or over `hard_limit` of the window is not sent. The workflow handles this
        as it does the provider's own answer (F6), by compacting and asking once more. `measured`
        is the size of this very request when the caller has it already.
        """
        budget = self._budget
        if budget is None:
            return 0
        if measured is not None:
            tokens = measured
        else:
            tokens = await self._measure(provider, cfg, system, request, tools, meter)
        if budget.decide(tokens) == "force":
            limit = int(budget.window * budget.hard_limit)
            raise provider_failure(
                ProviderError(
                    ProviderErrorKind.CONTEXT_TOO_LONG,
                    f"the request is about {tokens} tokens, over the hard limit of {limit} tokens "
                    f"({budget.hard_limit} of a window of {budget.window})",
                )
            )
        return tokens

    async def _exact_count(
        self,
        provider: LLMProvider,
        cfg: LoopConfig,
        system: str,
        request: list[Message],
        tools: list[ToolSpec],
    ) -> int | None:
        """The provider's count of the request, or None to keep the estimate.

        The count is asked for the request that is sent, with the same options, because counts
        depend on the model and on the request rules. A failed or empty count leaves the
        estimate, and a request that really is too long fails on the call and is compacted then.
        """
        try:
            async with heartbeating():
                count = await provider.count_tokens(
                    model=cfg.model,
                    system=system,
                    messages=request,
                    tools=tools,
                    options=_options(cfg),
                )
        except ProviderError:
            return None
        return count if count > 0 else None

    def _decide(
        self,
        meter: TokenMeter,
        system: str,
        tools: list[ToolSpec],
        view: list[Message],
        calls: list[ToolCall],
        mode: ContextMode,
    ) -> tuple[Decision, int]:
        """What to do before the next turn, and how large that request is expected to be.

        The decision comes from the projection of the request after this one.

        That request has one more result elided than this one (eviction runs at the start of a
        turn, and the reply just made ages the others), and it holds the results of the calls
        just made. Those are not known yet, and each is at most tool_output_max_tokens, so that
        is what is reserved. A `finish` call has no result to reserve for. A mode without
        compaction has nothing to do before the next turn, whatever the projection says, and the
        size is still returned, for the budget check. It is 0 without a context policy.
        """
        policy, budget = self._context, self._budget
        if policy is None or budget is None:
            return "ok", 0
        upcoming = elide_old_outputs(view, policy.elide_tool_outputs_after_turns)
        pending = sum(1 for call in calls if not _answered_by_the_workflow(call.name))
        tokens = meter.projected(system, upcoming, tools) + pending * policy.tool_output_max_tokens
        return (budget.decide(tokens) if mode.compacts else "ok"), tokens

    # ----- the call -----
    async def _ask(
        self,
        provider: LLMProvider,
        inp: acts.LlmTurnInput,
        cfg: LoopConfig,
        system: str,
        request: list[Message],
        tools: list[ToolSpec],
        attempt: int,
        started: datetime,
    ) -> tuple[LLMResponse, Decimal]:
        """Call the model once and put the call on the ledger. Returns the reply and its cost.

        The ledger row is written inside the chat span, as soon as the call returns, so the span
        carries the same price as the row.
        """
        cap = cfg.max_tokens_retry if inp.high_output_cap else cfg.max_tokens_per_turn
        options = _options(cfg)
        try:
            with chat_span(
                model=cfg.model,
                provider=provider.name,
                task_id=inp.task_id,
                turn_index=inp.turn_index,
                role=inp.role,
                system=system,
                messages=request,
            ) as span:
                async with heartbeating():
                    resp = await llm_step(provider, cfg.model, system, request, tools, cap, options)
                cost = await self._charge(inp, cfg.model, resp.usage)
                span.record(resp, cost_usd=float(cost))
            metrics.current().count_turn(inp.role, resp.usage)
            return resp, cost
        except ProviderError as exc:
            metrics.current().count_llm_error(exc.kind.value)
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
            cost = await self._ledger.record(
                inp.task_id, inp.spec.repo, model, inp.turn_index, activity.info().attempt, usage
            )
        except PricingError as exc:
            raise not_retried(exc) from exc
        # The metric follows the ledger, so a call that is not on the ledger adds no cost.
        metrics.current().add_cost(model, inp.spec.repo, float(cost))
        return cost

    # ----- what came back -----
    async def _store(
        self,
        inp: acts.LlmTurnInput,
        resp: LLMResponse,
        view: _View,
        attempt: int,
        cost: Decimal,
        system: str,
        tools: list[ToolSpec],
        meter: TokenMeter,
        projected: int,
        mode: ContextMode,
    ) -> acts.LlmTurnOutput:
        paused = view.paused
        complete = rules.is_complete(resp)
        reply = rules.join_pauses(resp, paused)
        grown = self._grow(view.sent, reply) if complete else view.sent
        calls = rules.runnable_calls(resp, resp.tool_calls)

        def key(name: str) -> str:
            return blob_key(inp.task_id, "turns", inp.turn_index, name)

        response_ref = await self._blobs.put_json(resp, key(f"response-{attempt}.json"))
        if len(grown) == len(view.sent) and inp.transcript_ref is not None:
            # The reply added nothing, so there is nothing to store. A hint that joined the last
            # message stays out as well, and the next call adds it again.
            transcript_ref = inp.transcript_ref
        else:
            transcript_ref = await self._blobs.put_json(grown, key(f"transcript-{attempt}.json"))
        if len(grown) > len(view.sent):
            # The log gets the same step without the elision: the hints shown, then the reply.
            # Only the stored view was elided, so the reply is the last message of both.
            added = [*added_messages(view.before, view.shown), grown[-1]]
            await append_to_log(self._blobs, inp.task_id, inp.turn_index, "reply", added)
        tool_calls_ref = None
        if calls:
            tool_calls_ref = await self._blobs.put_json(calls, key(f"tool_calls-{attempt}.json"))
        carried = [*paused, *rules.carried_text(resp)] if resp.stop_reason == "pause_turn" else []
        paused_ref = None
        if carried:
            paused_ref = await self._blobs.put_json(carried, key(f"paused-{attempt}.json"))
        usage = resp.usage
        decision, next_tokens = self._decide(meter, system, tools, grown, calls, mode)
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
            context_decision=decision,
            request_tokens=projected,
            next_request_tokens=next_tokens,
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
