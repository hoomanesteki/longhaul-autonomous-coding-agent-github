"""TaskWorkflow: the one durable owner of a task.

It holds the task's CarryState, answers `status()`, takes the signals that steer a task, and
drives the phases through activities: PREPARING, a CODING loop, then DONE. All I/O happens in
activities, which this module calls by name through longhaul.durable.acts. It imports no I/O
library (invariant I1, checked by tests/unit/test_invariants.py), reads time only through
workflow.now() (it needs none yet), and uses no random numbers.

The CODING loop applies the same turn rules as the in-process loop. The decisions are in
longhaul.agent.rules, which both call, so they cannot drift (tests/integration/test_parity.py
runs the same scripts through both and compares the transcripts). The workflow only supplies
what differs: a model call is an `llm_turn` activity, a tool call is a `tool_exec` activity, and
the transcript lives in the blob store, so an `append_results` activity edits it.

A turn goes like this. `llm_turn` makes one model call and returns its stop reason. The rules
say what that leads to: use the reply, ask again once at the higher output cap (D8), continue a
paused reply, or end the run (a refusal, a full window, a second cut, an unknown stop). A used
reply with tool calls has them run one after another by `tool_exec`, and `append_results`
answers them all in one message. A used reply with none gets a nudge, and a second one in a row
ends the run.

The workflow keeps `state.head_sha` equal to the latest `post_sha` and passes it as `pre_sha` to
every `tool_exec`, so each call starts from the checkpoint the one before it left (ADR-0006).

A task started with a script in its memo (acts.SCRIPT_MEMO_KEY, from `longhaul run --script`)
hands that ref to every `llm_turn`, and the activity plays it instead of the provider of the
worker. The workflow only passes the ref on. Reading it is the activity's job.

A sandbox that goes away (failure mode F3). A `tool_exec` that finds its container gone fails
with SandboxGoneError, which the activity does not retry. The workflow calls `ensure_sandbox`
with the latest `head_sha`, stores the id it returns and runs the same call again from the same
`pre_sha`. One call may do that MAX_SANDBOX_RECREATIONS times. A sandbox that is gone again after
that ends the run with ERROR "sandbox lost", instead of looping on a host that cannot keep a
container alive. The end still answers every call of the turn through `append_results`: the
calls that ran keep their results and the rest get an error result, so the stored transcript
has no tool_use without a tool_result. A run that starts with a sandbox id, because an earlier
run prepared it and handed over (see Continue-As-New below), calls `ensure_sandbox` before its
first turn, since the container may be gone by then.

How a run ends. `finish` is the one good end. Every other end has a fixed summary, so a caller
can tell them apart without reading prose:

- the model calls `finish` with a summary: PR_OPENED with the model's summary and no pr_url, since
  opening the pull request is Phase 3 work. This is the same outcome the in-process loop returns;
- a `cancel` signal: CANCELLED, after a `cleanup` activity;
- `task_turns` plus the turns added by `add_budget` pass: ERROR "turn limit";
- a tool call finds its sandbox gone more often than it may: ERROR "sandbox lost";
- the other ends of agent/rules.py: "no tool calls", "output limit", "model declined (...)",
  "context limit", "pause limit" and "unexpected stop reason: ...".

Not here yet, each in its own story: the planner and verifier phases (Phase 5), budget
enforcement and pauses (Phase 7), clean-up and a final outcome for runs that end any way but a
cancel (LH-507). An activity that fails for good, after its retries, fails the workflow with that
error. A prompt over the context window is the exception, and it is handled as described next.

The context tiers (LH-404, ADR-0013). The workflow cannot count tokens, so each `llm_turn` returns
a decision for the turn after it: "ok", "compact" or "force" (acts.LlmTurnOutput.context_decision).
When the turn is over and its results are in the transcript, the workflow acts on it, in
`_relieve`. "compact" and "force" call the `compact` activity, which replaces the older turns of
the view with progress notes. The workflow stores the new view in `state.transcript_ref` and the
notes in `state.notes`, adds the cost of the compactor, and records a `compaction` event with the
tokens before and after. Doing it at the end of the turn means a hand-over that follows carries
the compacted view and its notes. A compaction that fails for good fails the workflow, like any
other activity, and one that finds nothing to fold changes nothing and records nothing.

F6, a prompt over the window. `llm_turn` refuses a request that its projection puts over the hard
limit, and the provider can reject one too. Both arrive as a provider error of kind
context_too_long. The workflow then compacts with `forced=True`, which keeps the last two turns
and the notes, and asks the turn again, once. It does not ask again when the compaction folded
nothing, because the view would be the same. A second overflow in the same turn, or one with
nothing to fold (the first turn has no view at all), ends the run with "context limit" as before.

The context modes (LH-406). `context.mode` of the worker's config, or the mode a run asks for with
`longhaul run --context-mode`, switches the tiers off for an ablation: `no_compaction` keeps tier 1
and `none` has neither (longhaul.context.modes). The workflow cannot read the config, so a run's
own mode comes from the memo (acts.CONTEXT_MODE_MEMO_KEY). The workflow hands it to every `llm_turn`
and `compact`, which resolve it against the config, and it skips the `compact` call itself when the
memo says the mode has no compaction. When the memo is empty the config decides, and the workflow
asks as it always did: `compact` answers "unchanged" in a mode without compaction, and
`_compact` reads that as nothing to fold. A prompt over the hard limit then ends the run with
"context limit" at once, since there is nothing to relieve it with. The memo is read and no
command is added, so the histories of runs before this story replay as they were.

Continue-As-New (failure mode F12). A run keeps its history short by handing over to a new run of
the same workflow id. After a turn that did not end the run, the workflow hands over when the
turns done are a multiple of the interval (acts.CONTINUE_AS_NEW_EVERY_TURNS, 40, or the run's
Tuning), or when the server suggests it (workflow.info().is_continue_as_new_suggested()). A run
that has used its turns ends with "turn limit" instead, because the new run would only find that
out after a bundle upload. The hand-over goes in this order:

1. `bundle_checkpoint` uploads the history of the checkpoint repo, so that a lost host costs at
   most the work since the last hand-over (failure mode F4);
2. `record_event` writes a `continue_as_new` event (a local activity);
3. the workflow waits for its signal handlers to finish (workflow.all_handlers_finished);
4. it looks at the cancel request one last time and calls continue_as_new with its state. Nothing
   is awaited between that look and the call, so a signal cannot land in the gap. A signal that
   reaches the server together with the command makes the server refuse the command, and the SDK
   runs the workflow task again with the signal in the history (ADR-0004, appendix section 4).

The bundle and the event are best effort. They are a record for people and a safety net for a
lost host, and a task that failed because its log was unreachable would lose far more than they
protect. A failure is logged and the hand-over goes on, since the history has to be reset.

What the new run starts with. The state travels in CarryState: the turn index, the transcript, the
checkpoint, the sandbox id, the cost, the hints and the budget top-ups. `hint` and `add_budget`
write to it, so everything that arrived before the hand-over is in it, and a signal that arrives
later goes to the new run (same id; the state is built in @workflow.init before any handler runs,
so none is dropped). The Tuning is in the memo, which continue_as_new carries over. The new run
calls `ensure_sandbox` first, like any run that starts with a sandbox id (LH-206).

Hints stay in `state.hints` for the life of the task. Each model call gets all of them, and
`llm_turn` shows the model the ones its transcript does not hold yet (turns.show_hints). The
transcript is the record of what the model has read, so no marker has to cross the hand-over, and
a hint signalled just before it is in the first prompt of the next run.

What does not cross, and why that is fine:

- A cancel is not carried. One that has arrived by the last look is acted on in this run, with the
  clean-up and the CANCELLED outcome. Any later one reaches the new run as a signal.
- The count of turns without a tool call (`self.idle`) is not in CarryState, which this story may
  not change. After a hand-over that follows a nudge, the model gets one more nudge before the run
  ends with "no tool calls".
- `plan_decision` is not carried. The plan gate (Phase 5) waits and reads the decision inside one
  run, before any hand-over, and CarryState keeps `plan_approved`.
- `outcome` is never set at a hand-over, because a run that has an outcome ends.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, NoReturn

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, FailureError

# Contracts, acts and the turn rules are pure. Passing them through the sandbox saves about
# 30 ms per run and keeps one class object for each model, so an isinstance check against a
# model that came from outside still works (ADR-0004, appendix section 1).
with workflow.unsafe.imports_passed_through():
    from longhaul.agent import rules
    from longhaul.context.modes import ContextMode
    from longhaul.context.tokens import Decision
    from longhaul.contracts import (
        BlobRef,
        CarryState,
        Outcome,
        OutcomeKind,
        Phase,
        ProviderErrorKind,
        Role,
        TaskStatus,
        ToolResult,
    )
    from longhaul.durable import acts


@dataclass(frozen=True)
class CancelRequest:
    by: str
    reason: str


@dataclass(frozen=True)
class PlanDecision:
    approved: bool
    by: str
    feedback: str = ""


def cancel_summary(by: str, reason: str) -> str:
    reason = reason.strip()
    return f"cancelled by {by}: {reason}" if reason else f"cancelled by {by}"


# The summary of the outcome when one tool call keeps finding its sandbox gone.
SANDBOX_LOST = "sandbox lost"

# The event a compaction records (plan section 3.5).
COMPACTION_EVENT = "compaction"

# The event a hand-over to a new run records (plan section 3.5), and the two reasons it gives.
CONTINUE_AS_NEW_EVENT = "continue_as_new"
HAND_OVER_TURNS = "turns"  # the turn count reached the interval
HAND_OVER_HISTORY = "history"  # the server suggested it, because the history grew


class _CancelRequested(Exception):
    """Unwinds run() to the cancel path. It never leaves the workflow."""


# What the transcript says for the calls of a turn that ended in a lost sandbox. Nothing reads
# these to decide anything. They keep every tool_use answered, as the API requires.
LOST_CALL = "the sandbox was lost and could not be recreated, so this call did not finish"
NOT_RUN_AFTER_LOSS = "not run: an earlier call of this turn lost the sandbox for good"


class _SandboxLost(Exception):
    """A tool call found its sandbox gone more often than it may. It unwinds to _turn()."""

    def __init__(self, call_id: str) -> None:
        super().__init__(call_id)
        self.call_id = call_id


def _is_sandbox_gone(err: ActivityError) -> bool:
    """True when a tool call failed because its container was not there.

    The activity raises that as an ApplicationError of type SANDBOX_GONE, so the cause says it.
    """
    cause = err.cause
    return isinstance(cause, ApplicationError) and cause.type == acts.SANDBOX_GONE


def _provider_error_kind(err: ActivityError) -> ProviderErrorKind | None:
    """The kind of the provider error that failed a model turn, or None when it is not one.

    The activity raises a provider error as an ApplicationError whose type is the error kind
    (support.provider_failure), so the cause tells a prompt over the window from the rest.
    """
    cause = err.cause
    if not isinstance(cause, ApplicationError) or cause.type is None:
        return None
    try:
        return ProviderErrorKind(cause.type)
    except ValueError:
        return None


def _ended_by(err: ActivityError) -> Outcome | None:
    """The outcome a failed model turn ends the run with, or None when the failure is not one."""
    kind = _provider_error_kind(err)
    return None if kind is None else rules.provider_error_outcome(kind)


@workflow.defn(name="TaskWorkflow")
class TaskWorkflow:
    # @workflow.init runs before any signal handler, also for a signal that arrives in the
    # same first workflow task as the start. Building the state in run() lost most of the
    # signals sent while a workflow was starting (ADR-0004, appendix section 4).
    @workflow.init
    def __init__(self, state: CarryState) -> None:
        self.state = state
        self.outcome: Outcome | None = None
        self.cancel_request: CancelRequest | None = None
        self.plan_decision: PlanDecision | None = None
        self.idle = 0  # turns in a row that ended without a tool call

    # ---------- signals and the query ----------
    @workflow.signal
    def cancel(self, by: str, reason: str) -> None:
        # The first request wins, so a second cancel cannot change who is named in the outcome.
        if self.cancel_request is None:
            self.cancel_request = CancelRequest(by, reason)

    @workflow.signal
    def hint(self, by: str, text: str) -> None:
        self.state.hints.append(f"{by}: {text}")

    @workflow.signal
    def add_budget(self, by: str, usd: float, turns: int) -> None:
        # A negative amount would lower a budget the task already has. Each amount is judged
        # on its own, so the valid half of a mixed top-up still counts.
        self.state.extra_budget_usd += max(usd, 0.0)
        self.state.extra_turns += max(turns, 0)

    @workflow.signal
    def approve_plan(self, by: str) -> None:
        self.state.plan_approved = True
        self.plan_decision = PlanDecision(approved=True, by=by)

    @workflow.signal
    def reject_plan(self, by: str, feedback: str) -> None:
        self.state.plan_approved = False
        self.plan_decision = PlanDecision(approved=False, by=by, feedback=feedback)

    @workflow.query
    def status(self) -> TaskStatus:
        state = self.state
        return TaskStatus(
            task_id=state.spec.task_id,
            phase=state.phase,
            turn_index=state.turn_index,
            cost_usd=state.cost_usd,
            last_checkpoint=state.head_sha,
            # CarryState counts from 0 for "no plan yet", TaskStatus says None.
            plan_version=state.plan_version or None,
            outcome=self.outcome,
        )

    # ---------- the run ----------
    @workflow.run
    async def run(self, state: CarryState) -> Outcome:
        # The input is already in self.state. The SDK needs the two signatures to match.
        try:
            await self._prepare()
            return await self._code()
        except _CancelRequested:
            return await self._cancelled()

    async def _prepare(self) -> None:
        state = self.state
        state.phase = Phase.PREPARING
        if state.sandbox_id is not None:
            # An earlier run of this workflow prepared the workspace, and its container may be
            # gone by now. ensure_sandbox answers with the same id when it is still running.
            await self._ensure_sandbox()
            return
        prepared = await self._call(acts.PREPARE_WORKSPACE, state.spec)
        state.sandbox_id = prepared.sandbox_id
        state.head_sha = prepared.head_sha

    async def _ensure_sandbox(self) -> None:
        """Have a running sandbox at the latest checkpoint, and keep its id."""
        state = self.state
        if state.head_sha is None:
            # Without a checkpoint there is nothing to return to. Asking again changes nothing.
            raise ApplicationError(
                "the sandbox cannot be recreated: the run has no checkpoint", non_retryable=True
            )
        state.sandbox_id = await self._call(
            acts.ENSURE_SANDBOX,
            acts.EnsureSandboxInput(
                task_id=state.spec.task_id,
                head_sha=state.head_sha,
                sandbox_id=state.sandbox_id,
                spec=state.spec,
            ),
        )

    async def _code(self) -> Outcome:
        state = self.state
        state.phase = Phase.CODING
        while True:
            if self._out_of_turns():
                return self._end(rules.turn_limit_outcome(), Phase.FAILED)
            outcome = await self._turn()
            if outcome is not None:
                return outcome
            reason = self._hand_over_reason()
            if reason is not None and not self._out_of_turns():
                await self._hand_over(reason)

    def _out_of_turns(self) -> bool:
        # add_budget can raise the limit while the task runs, so it is read each time.
        state = self.state
        return state.turn_index >= state.spec.settings.budgets.task_turns + state.extra_turns

    def _hand_over_reason(self) -> str | None:
        """Why the run should hand over to a new one now, or None when it should go on."""
        turns, tuning = self.state.turn_index, self._tuning()
        suggested = workflow.info().is_continue_as_new_suggested()
        if not acts.hand_off_due(turns, tuning, suggested=suggested):
            return None
        return HAND_OVER_TURNS if acts.hand_off_due(turns, tuning) else HAND_OVER_HISTORY

    async def _hand_over(self, reason: str) -> NoReturn:
        """End this run and start the next one with the same state (see the module docstring).

        continue_as_new raises ContinueAsNewError, which is a BaseException. Nothing between here
        and the call may catch it or await in a `finally`.
        """
        state = self.state
        bundle = await self._bundle()
        await self._note(
            CONTINUE_AS_NEW_EVENT,
            {
                "reason": reason,
                "turn_index": state.turn_index,
                "head_sha": state.head_sha,
                "bundle": bundle,
            },
        )
        await workflow.wait_condition(workflow.all_handlers_finished)
        # A cancel that came in during the awaits above must not be lost with this run. It is
        # acted on here. No await follows, so none can arrive between this look and the call.
        self._stop_if_cancelled()
        workflow.continue_as_new(state)

    async def _bundle(self) -> str | None:
        """Upload the repo history. Returns the key of the blob, or None when it was not made."""
        state = self.state
        try:
            ref = await self._call(
                acts.BUNDLE_CHECKPOINT,
                acts.BundleCheckpointInput(task_id=state.spec.task_id, turn_index=state.turn_index),
            )
        except ActivityError as err:
            workflow.logger.warning(
                "no bundle of task %s before the hand-over: %s",
                state.spec.task_id,
                err.cause or err,
            )
            return None
        return ref.key

    async def _note(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record an event. A log line that cannot be written does not fail the task."""
        task_id = self.state.spec.task_id
        try:
            await self._call(
                acts.RECORD_EVENT,
                acts.RecordEventInput(task_id=task_id, type=event_type, payload=payload),
            )
        except FailureError as err:
            # A local activity that fails raises its own error, an ApplicationError, and not the
            # ActivityError that a remote one is wrapped in.
            workflow.logger.warning(
                "event %s of task %s not recorded: %s", event_type, task_id, err.cause or err
            )

    async def _turn(self) -> Outcome | None:
        """One model turn and what follows it. Returns the outcome when the turn ends the run."""
        state = self.state
        index = state.turn_index
        sampled = await self._sample(index)
        if isinstance(sampled, Outcome):
            state.turn_index = index + 1
            return self._end(sampled, Phase.FAILED)
        calls = rules.runnable_calls(sampled, sampled.tool_call_summaries)
        if not calls:
            return await self._idle_turn(index, sampled)

        self.idle = 0
        finished: list[ToolResult] = []  # kept for the transcript if a later call loses the sandbox

        async def run(call: acts.ToolCallSummary) -> ToolResult:
            result = await self._run_tool(index, call)
            finished.append(result)
            return result

        try:
            ran = await rules.run_calls(calls, run)
        except _SandboxLost as lost:
            return await self._end_lost(index, sampled.transcript_ref, calls, finished, lost)
        # The transcript lives in the blob store, so an activity answers the calls. It answers
        # all of them, `finish` and the calls that did not run included, so the transcript stays
        # paired when the run ends here.
        state.transcript_ref = await self._call(
            acts.APPEND_RESULTS,
            acts.AppendResultsInput(
                task_id=state.spec.task_id,
                turn_index=index,
                transcript_ref=sampled.transcript_ref,
                results=[acts.summarize_result(r) for r in ran.results],
            ),
        )
        state.turn_index = index + 1
        if ran.finish is not None:
            return self._end(rules.finished(ran.finish), Phase.DONE)
        await self._relieve(index, sampled.context_decision)
        return None

    async def _end_lost(
        self,
        index: int,
        transcript_ref: BlobRef,
        calls: list[acts.ToolCallSummary],
        finished: list[ToolResult],
        lost: _SandboxLost,
    ) -> Outcome:
        """End the run for a sandbox that keeps dying, with every call of the turn answered.

        The calls that ran keep their results. The one that lost the sandbox and the ones after
        it get an error result, so the stored transcript has no tool_use without a tool_result.
        """
        state = self.state
        done = {r.call_id: r for r in finished}
        answers = [
            acts.summarize_result(done[call.id])
            if call.id in done
            else acts.ToolResultSummary(
                call_id=call.id,
                ok=False,
                output=LOST_CALL if call.id == lost.call_id else NOT_RUN_AFTER_LOSS,
            )
            for call in calls
        ]
        state.transcript_ref = await self._call(
            acts.APPEND_RESULTS,
            acts.AppendResultsInput(
                task_id=state.spec.task_id,
                turn_index=index,
                transcript_ref=transcript_ref,
                results=answers,
            ),
        )
        state.turn_index = index + 1
        return self._end(rules.error(SANDBOX_LOST), Phase.FAILED)

    async def _sample(self, index: int) -> acts.LlmTurnOutput | Outcome:
        """Call the model until one reply can be used, or the run has to end (D8, D9).

        A prompt that is over the window is compacted down to the last two turns, and the turn
        is asked once more (F6). It is asked again only if the compaction changed the view. A
        second overflow, or one with nothing to fold, ends the run with "context limit".
        """
        state = self.state
        sampling = rules.Sampling()
        paused_ref: BlobRef | None = None
        compacted = False
        while True:
            try:
                reply = await self._call(
                    acts.LLM_TURN,
                    acts.LlmTurnInput(
                        task_id=state.spec.task_id,
                        turn_index=index,
                        role=Role.CODER,
                        spec=state.spec,
                        transcript_ref=state.transcript_ref,
                        notes=state.notes,
                        plan=state.plan,
                        hints=list(state.hints),
                        high_output_cap=sampling.retried,
                        paused_ref=paused_ref,
                        script_ref=self._script(),
                        context_mode=self._context_mode(),
                    ),
                )
            except ActivityError as err:
                if (
                    _provider_error_kind(err) is ProviderErrorKind.CONTEXT_TOO_LONG
                    and not compacted
                ):
                    compacted = True
                    if await self._compact(index, forced=True):
                        continue
                outcome = _ended_by(err)
                if outcome is None:
                    raise
                return outcome
            # A call that was not used cost money all the same. The transcript is whatever the
            # activity hands back: unchanged when the reply was not stored.
            state.transcript_ref = reply.transcript_ref
            state.cost_usd += reply.cost_usd
            verdict = rules.judge(sampling, reply)
            sampling = verdict.sampling
            if verdict.outcome is not None:
                return verdict.outcome
            if verdict.move is rules.Move.CONTINUE:
                paused_ref = reply.paused_ref
            elif verdict.move is rules.Move.USE:
                return reply

    # ---------- the context tiers ----------
    async def _relieve(self, index: int, decision: Decision) -> None:
        """Compact the view when the model turn that just ended said the next request needs it.

        This runs when the turn is over and its results are in the view, so a hand-over that
        follows carries the compacted view and its notes.
        """
        if decision != "ok":
            await self._compact(index, forced=decision == "force")

    async def _compact(self, index: int, *, forced: bool) -> bool:
        """Fold the older turns of the view into notes. True when the view changed.

        The compactor is paid for either way, so its cost is added before anything else. A view
        with nothing to fold, or one that a failed compactor left as it was, changes nothing and
        records no event. The failure of the activity, after its retries, fails the workflow
        like any other activity does. A run whose mode has no compaction never calls it.
        """
        state = self.state
        mode = self._context_mode()
        if mode is not None and not mode.compacts:
            return False
        if state.transcript_ref is None:
            return False  # nothing has been said yet, so there is nothing to fold
        done = await self._call(
            acts.COMPACT,
            acts.CompactInput(
                task_id=state.spec.task_id,
                turn_index=index,
                role=Role.CODER,
                spec=state.spec,
                transcript_ref=state.transcript_ref,
                notes=state.notes,
                forced=forced,
                script_ref=self._script(),
                context_mode=mode,
            ),
        )
        state.cost_usd += done.cost_usd
        if done.folded_turns == 0:
            return False
        state.transcript_ref = done.transcript_ref
        state.notes = done.notes
        await self._note(
            COMPACTION_EVENT,
            {
                "turn_index": index,
                "forced": forced,
                "status": done.status,
                "tokens_before": done.tokens_before,
                "tokens_after": done.tokens_after,
                "folded_turns": done.folded_turns,
            },
        )
        return True

    async def _idle_turn(self, index: int, reply: acts.LlmTurnOutput) -> Outcome | None:
        """A reply with no tool calls. The first gets a nudge, and a second ends the run."""
        state = self.state
        idle = rules.idle_turn(self.idle)
        self.idle = idle.count
        if idle.outcome is not None:
            state.turn_index = index + 1
            return self._end(idle.outcome, Phase.FAILED)
        state.transcript_ref = await self._call(
            acts.APPEND_RESULTS,
            acts.AppendResultsInput(
                task_id=state.spec.task_id,
                turn_index=index,
                transcript_ref=reply.transcript_ref,
                results=[],
                user_text=rules.NUDGE,
            ),
        )
        state.turn_index = index + 1
        await self._relieve(index, reply.context_decision)
        return None

    async def _run_tool(self, index: int, call: acts.ToolCallSummary) -> ToolResult:
        state = self.state
        recreated = 0
        while True:
            try:
                result = await self._call(
                    acts.TOOL_EXEC,
                    acts.ToolExecInput(
                        task_id=state.spec.task_id,
                        turn_index=index,
                        call=call,
                        # Every call starts from the checkpoint the one before it left. After a
                        # recreation it is the same one, and the new sandbox is at it.
                        pre_sha=state.head_sha,
                        sandbox_id=self._sandbox_id(),
                    ),
                )
                break
            except ActivityError as err:
                if not _is_sandbox_gone(err):
                    raise
                if recreated == acts.MAX_SANDBOX_RECREATIONS:
                    workflow.logger.error(
                        "sandbox of task %s was gone %d times during %s, so the run ends",
                        state.spec.task_id,
                        recreated + 1,
                        call.name,
                    )
                    raise _SandboxLost(call.id) from err
                recreated += 1
                await self._ensure_sandbox()
        if result.post_sha is not None:
            state.head_sha = result.post_sha
        return result

    # ---------- cancel ----------
    async def _cancelled(self) -> Outcome:
        request = self.cancel_request
        if request is None:
            raise ApplicationError("cancel path without a cancel request", non_retryable=True)
        # Cleanup is not run through _call: a second cancel must not interrupt it.
        try:
            await workflow.execute_activity(
                acts.CLEANUP.name,
                acts.CleanupInput(
                    task_id=self.state.spec.task_id,
                    sandbox_id=self.state.sandbox_id,
                    reason=request.reason,
                ),
                result_type=acts.CLEANUP.result_type,
                **acts.options(acts.CLEANUP, self._tuning()),
            )
        except ActivityError as err:
            # The user asked to stop. A sandbox that will not go away must not keep the task
            # running, so the failure is logged and the run still ends as cancelled. The cause
            # is what says why; the error itself only says that the activity failed.
            workflow.logger.warning("cleanup after a cancel failed: %s", err.cause or err)
        outcome = Outcome(
            kind=OutcomeKind.CANCELLED, summary=cancel_summary(request.by, request.reason)
        )
        return self._end(outcome, Phase.CANCELLED)

    # ---------- helpers ----------
    async def _call[In, Out](self, act: acts.Act[In, Out], arg: In) -> Out:
        """Run an activity by name. A cancel signal interrupts it.

        A model turn can take ten minutes, and a person who asked to stop should not wait for
        it, so the activity is cancelled. The wait for it to stop ends in a heartbeat or two
        (see acts.options), and only then does the clean-up run.
        """
        self._stop_if_cancelled()
        if act.local:
            handle = workflow.start_local_activity(
                act.name, arg, result_type=act.result_type, **acts.local_options(act)
            )
        else:
            handle = workflow.start_activity(
                act.name, arg, result_type=act.result_type, **acts.options(act, self._tuning())
            )
        await workflow.wait_condition(lambda: handle.done() or self.cancel_request is not None)
        if handle.done():
            result: Out = await handle
            return result
        handle.cancel()
        # The handle resolves once the activity has stopped. It ends as cancelled, or as a
        # failure if the activity raised something else while it stopped, and either way the
        # run is over, so the outcome of the attempt does not matter. A local activity that
        # fails raises its own error and not an ActivityError, hence FailureError.
        with contextlib.suppress(FailureError, asyncio.CancelledError):
            await handle
        raise _CancelRequested

    def _stop_if_cancelled(self) -> None:
        if self.cancel_request is not None:
            raise _CancelRequested

    def _tuning(self) -> acts.Tuning | None:
        return workflow.memo_value(acts.TUNING_MEMO_KEY, None, type_hint=acts.Tuning)

    def _script(self) -> BlobRef | None:
        return workflow.memo_value(acts.SCRIPT_MEMO_KEY, None, type_hint=BlobRef)

    def _context_mode(self) -> ContextMode | None:
        """The context mode the run asked for, or None when the worker's config decides."""
        return workflow.memo_value(acts.CONTEXT_MODE_MEMO_KEY, None, type_hint=ContextMode)

    def _sandbox_id(self) -> str:
        sandbox_id = self.state.sandbox_id
        if sandbox_id is None:
            # run() prepares the workspace before the first turn. Failing the workflow with a
            # message is better than a workflow task that retries forever.
            raise ApplicationError("the workspace was not prepared", non_retryable=True)
        return sandbox_id

    def _end(self, outcome: Outcome, phase: Phase) -> Outcome:
        self.state.phase = phase
        self.outcome = outcome
        return outcome
