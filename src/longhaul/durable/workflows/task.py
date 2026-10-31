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

How a run ends. `finish` is the one good end. Every other end has a fixed summary, so a caller
can tell them apart without reading prose:

- the model calls `finish` with a summary: PR_OPENED with the model's summary and no pr_url, since
  opening the pull request is Phase 3 work. This is the same outcome the in-process loop returns;
- a `cancel` signal: CANCELLED, after a `cleanup` activity;
- `task_turns` plus the turns added by `add_budget` pass: ERROR "turn limit";
- the other ends of agent/rules.py: "no tool calls", "output limit", "model declined (...)",
  "context limit", "pause limit" and "unexpected stop reason: ...".

Not here yet, each in its own story: the planner and verifier phases (Phase 5), budget
enforcement and pauses (Phase 7), Continue-As-New (LH-207), clean-up and a final outcome for
runs that end any way but a cancel (LH-507). An activity that fails for good, after its retries,
fails the workflow with that error, except a prompt over the context window, which ends the run
with "context limit" as it does in-process.

The count of turns without a tool call (`self.idle`) belongs to the running workflow and is not
in CarryState, which the contracts do not let this story change. LH-207 has to carry it over a
Continue-As-New. Losing it there only gives the model one more nudge.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

# Contracts, acts and the turn rules are pure. Passing them through the sandbox saves about
# 30 ms per run and keeps one class object for each model, so an isinstance check against a
# model that came from outside still works (ADR-0004, appendix section 1).
with workflow.unsafe.imports_passed_through():
    from longhaul.agent import rules
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


class _CancelRequested(Exception):
    """Unwinds run() to the cancel path. It never leaves the workflow."""


def _ended_by(err: ActivityError) -> Outcome | None:
    """The outcome a failed model turn ends the run with, or None when the failure is not one.

    The activity raises a provider error as an ApplicationError whose type is the error kind
    (support.provider_failure), so the cause tells a prompt over the window from the rest.
    """
    cause = err.cause
    if not isinstance(cause, ApplicationError) or cause.type is None:
        return None
    try:
        kind = ProviderErrorKind(cause.type)
    except ValueError:
        return None
    return rules.provider_error_outcome(kind)


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
        if state.sandbox_id is not None:
            return  # an earlier run of this workflow already prepared it
        state.phase = Phase.PREPARING
        prepared = await self._call(acts.PREPARE_WORKSPACE, state.spec)
        state.sandbox_id = prepared.sandbox_id
        state.head_sha = prepared.head_sha

    async def _code(self) -> Outcome:
        state = self.state
        state.phase = Phase.CODING
        while True:
            # add_budget can raise the limit while the task runs, so it is read on each turn.
            if state.turn_index >= state.spec.settings.budgets.task_turns + state.extra_turns:
                return self._end(rules.turn_limit_outcome(), Phase.FAILED)
            outcome = await self._turn()
            if outcome is not None:
                return outcome

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
            return await self._idle_turn(index, sampled.transcript_ref)

        self.idle = 0
        ran = await rules.run_calls(calls, lambda call: self._run_tool(index, call))
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
        return None

    async def _sample(self, index: int) -> acts.LlmTurnOutput | Outcome:
        """Call the model until one reply can be used, or the run has to end (D8, D9)."""
        state = self.state
        sampling = rules.Sampling()
        paused_ref: BlobRef | None = None
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
                    ),
                )
            except ActivityError as err:
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

    async def _idle_turn(self, index: int, transcript_ref: BlobRef) -> Outcome | None:
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
                transcript_ref=transcript_ref,
                results=[],
                user_text=rules.NUDGE,
            ),
        )
        state.turn_index = index + 1
        return None

    async def _run_tool(self, index: int, call: acts.ToolCallSummary) -> ToolResult:
        state = self.state
        result = await self._call(
            acts.TOOL_EXEC,
            acts.ToolExecInput(
                task_id=state.spec.task_id,
                turn_index=index,
                call=call,
                # Every call starts from the checkpoint the one before it left.
                pre_sha=state.head_sha,
                sandbox_id=self._sandbox_id(),
            ),
        )
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
        # run is over, so the outcome of the attempt does not matter.
        with contextlib.suppress(ActivityError, asyncio.CancelledError):
            await handle
        raise _CancelRequested

    def _stop_if_cancelled(self) -> None:
        if self.cancel_request is not None:
            raise _CancelRequested

    def _tuning(self) -> acts.Tuning | None:
        return workflow.memo_value(acts.TUNING_MEMO_KEY, None, type_hint=acts.Tuning)

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
