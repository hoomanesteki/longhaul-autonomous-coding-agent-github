"""TaskWorkflow: the one durable owner of a task.

It holds the task's CarryState, answers `status()`, takes the signals that steer a task, and
drives the phases through activities: PREPARING, a CODING loop, then DONE. All I/O happens in
activities, which this module calls by name through longhaul.durable.acts. It imports no I/O
library (invariant I1, checked by tests/unit/test_invariants.py), reads time only through
workflow.now() (it needs none yet), and uses no random numbers.

How a run ends. `finish` is the one good end. Every other end has a fixed summary, so a caller
can tell them apart without reading prose:

- the model calls `finish`: PR_OPENED with the model's summary and no pr_url, since opening
  the pull request is Phase 3 work. This is the same outcome the in-process loop returns;
- a `cancel` signal: CANCELLED, after a `cleanup` activity;
- `task_turns` plus the turns added by `add_budget` pass: ERROR "turn limit";
- a turn ends without a tool call: ERROR "no tool calls".

Not here yet, each in its own story: the planner and verifier phases (Phase 5), budget
enforcement and pauses (Phase 7), Continue-As-New (LH-207), clean-up and a final outcome for
runs that end any way but a cancel (LH-507), and the shared turn rules of the in-process loop
(D8 output retry, D9 refusal, the nudge), which arrive with the llm_turn activity in LH-204.
An activity that fails for good, after its retries, fails the workflow with that error.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

# Contracts and acts are pure. Passing them through the sandbox saves about 30 ms per run and
# keeps one class object for each model, so an isinstance check against a model that came
# from outside still works (ADR-0004, appendix section 1).
with workflow.unsafe.imports_passed_through():
    from longhaul.contracts import (
        CarryState,
        Outcome,
        OutcomeKind,
        Phase,
        Role,
        TaskStatus,
        ToolResult,
    )
    from longhaul.durable import acts

TURN_LIMIT = "turn limit"
NO_TOOL_CALLS = "no tool calls"


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
                return self._end(Outcome(kind=OutcomeKind.ERROR, summary=TURN_LIMIT), Phase.FAILED)
            outcome = await self._turn()
            if outcome is not None:
                return outcome

    async def _turn(self) -> Outcome | None:
        """One model turn and its tool calls. Returns the outcome when the turn ends the run."""
        state = self.state
        index = state.turn_index
        turn = await self._call(
            acts.LLM_TURN,
            acts.LlmTurnInput(
                task_id=state.spec.task_id,
                turn_index=index,
                role=Role.CODER,
                transcript_ref=state.transcript_ref,
                notes=state.notes,
                plan=state.plan,
                hints=list(state.hints),
            ),
        )
        state.transcript_ref = turn.transcript_ref
        state.cost_usd += turn.cost_usd

        results, summary = await self._run_tools(index, turn.tool_call_summaries)
        if summary is None and results:
            # The transcript lives in the blob store, so an activity appends the results.
            state.transcript_ref = await self._call(
                acts.APPEND_RESULTS,
                acts.AppendResultsInput(
                    task_id=state.spec.task_id,
                    turn_index=index,
                    transcript_ref=turn.transcript_ref,
                    results=results,
                ),
            )
        state.turn_index = index + 1

        if summary is not None:
            return self._end(Outcome(kind=OutcomeKind.PR_OPENED, summary=summary), Phase.DONE)
        if not results:
            return self._end(Outcome(kind=OutcomeKind.ERROR, summary=NO_TOOL_CALLS), Phase.FAILED)
        return None

    async def _run_tools(
        self, index: int, calls: list[acts.ToolCallSummary]
    ) -> tuple[list[ToolResult], str | None]:
        """Run the calls one after another. A `finish` call stops the turn and gives its summary."""
        state = self.state
        results: list[ToolResult] = []
        for call in calls:
            if call.name == acts.FINISH_TOOL:
                return results, acts.finish_summary(call)
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
            results.append(result)
        return results, None

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
