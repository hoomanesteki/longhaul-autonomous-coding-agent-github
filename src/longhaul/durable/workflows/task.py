"""TaskWorkflow: the one durable owner of a task.

It holds the task's CarryState, answers `status()`, takes the signals that steer a task, and
drives the phases through activities: PREPARING, PLANNING when the task asks for it, a CODING loop,
then DONE. The task of an issue goes through DELIVERING before DONE (see "A task of an issue"
below). All I/O happens in activities, which this module calls by name through
longhaul.durable.acts. It imports no I/O library (invariant I1, checked by
tests/unit/test_invariants.py), reads time only through workflow.now() (it needs none yet), and
uses no random numbers.

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

The planning phase (LH-501, ADR-0007). A task started with the plan switch (acts.PLAN_MEMO_KEY)
runs the planner between PREPARING and CODING, in the same workflow, the same sandbox and the same
blob store as the coder. The planner has a role prompt, a tool list and a transcript of its own, so
the coder starts a fresh transcript and sees none of the planner's reading. The turn index goes on
counting, because turn rows and blobs are keyed by it. A task without the switch goes straight to
coding.

1. Phase PLANNING. Each turn is a `llm_turn` with `Role.PLANNER`. Its tool calls run through
   `tool_exec` marked read-only: the activity puts the tree back on the checkpoint after any call
   that changed it, so the coder starts from the tree the task began with. A call to a tool outside
   the planner's set is answered without running (roles/planner.py).
2. The planner ends by calling `submit_plan`. The workflow judges the call itself with the rules of
   roles/planner.py, as it does `finish`. A plan that is not valid gets one repair turn with the
   validation error in front of the planner (F14), and a second failure ends the task with ERROR
   "planner submitted an invalid plan twice: ...". A planner that has not submitted after
   acts.PLANNER_MAX_TURNS turns ends it with ERROR "planner submitted no plan in 20 turns", and the
   turn budget of the task applies to the planner's turns as to the coder's.
3. A plan that is accepted is stored as version 1 by the `store_plan` activity, and a task of an
   issue posts it as a checklist with `post_plan` (key `<task>:plan_comment:1`). Then it is in
   `state.plan` and `state.plan_version`, and `status()` shows the version. The phase moves on to
   CODING, with a new transcript.

A run that starts with a plan, because an earlier run made it and handed over, does not plan
again.

A task of an issue (LH-306) is one whose repo is owner/name and whose spec has an issue number
(lifecycle.is_github_task). It differs from a local task in three places and nowhere else:

1. Its first activity is the pickup: the "Picked up" comment and the `longhaul:working` label on
   the issue, both written through github/side_effects.py with stable keys. A run that starts
   with a sandbox id, because an earlier run prepared it and handed over, has been picked up
   already and does not repeat it. The keys would make a repeat harmless, but each one costs an
   activity and a place in the history.
2. When the model calls `finish`, the phase becomes DELIVERING and the task is not over. The
   `build_pr_branch` activity makes the pull request branch on the base the workspace was
   prepared at (kept in `state.spec.base_sha`, because the `base` tag in the checkpoint repo is
   the agent's to move), and `deliver` pushes it and opens a draft pull request. The task then
   swaps the label (`longhaul:working` out, `longhaul:pr-open` in) and ends with PR_OPENED
   carrying the URL. `status()` shows no outcome until then.
3. A task whose work changes no file ends with ERROR "nothing to deliver: no file changed",
   because GitHub refuses a pull request with no commit.

Pause and resume (Phases 5 and 7) add and remove `longhaul:needs-human` through `_needs_human`
and `_human_done`, which are here for them. Nothing calls them yet. The labels of a task that
ends any other way (an error, a cancel) are left alone until LH-507.

`/longhaul status` on the issue reaches the task as the signal `report_status` (LH-308). The
handler posts a comment with the state of the task through the `post_comment` activity, so it is
a side effect with a key like any other, `status:<n>`, where n counts the requests the task has had.
The count is part of the workflow's state, so a replay posts nothing again, and the memo carries it
over a Continue-As-New. The handler is best effort: a status that cannot be posted is logged and
does not fail the task, and a cancel does not wait for it, not in a turn and not at a hand-over
(see `_settle_handlers`). It posts for a task of an issue only.

How a run ends. `finish` is the one good end. Every other end has a fixed summary, so a caller
can tell them apart without reading prose:

- the model calls `finish` with a summary: PR_OPENED with the model's summary. A local task has
  no pull request and so no pr_url, which is the outcome the in-process loop returns too. The
  task of an issue has the URL of its pull request;
- a `cancel` signal: CANCELLED, after a `cleanup` activity;
- `task_turns` plus the turns added by `add_budget` pass: ERROR "turn limit";
- a tool call finds its sandbox gone more often than it may: ERROR "sandbox lost";
- the other ends of agent/rules.py: "no tool calls", "output limit", "model declined (...)",
  "context limit", "pause limit" and "unexpected stop reason: ...".

The planner adds two ends of its own: ERROR "planner submitted an invalid plan twice: ..." and
ERROR "planner submitted no plan in 20 turns".

Not here yet, each in its own story: the plan gate, the coder's use of the plan and the verifier
phase (Phase 5), budget enforcement and pauses (Phase 7), clean-up and a final outcome for runs
that end any way but a cancel (LH-507). An activity that fails for good, after its retries, fails
the workflow with that error. A prompt over the context window is the exception, and it is
handled as described next.

The context tiers (LH-404, ADR-0013). The workflow cannot count tokens, so each `llm_turn` returns
a decision for the turn after it: "ok", "compact" or "force" (acts.LlmTurnOutput.context_decision).
When the turn is over and its results are in the transcript, the workflow acts on it, in
`_relieve`. "compact" and "force" call the `compact` activity, which replaces the older turns of
the view with progress notes. The workflow stores the new view in `state.transcript_ref` and the
notes in `state.notes`, adds the cost of the compactor, and records a `compaction` event with the
tokens before and after. Doing it at the end of the turn means a hand-over that follows carries
the compacted view and its notes. A compaction that fails for good fails the workflow, like any
other activity, and one that finds nothing to fold changes nothing and records nothing.

A run can ask for a window of its own (`longhaul run --window`, acts.Tuning.window_tokens). The
workflow cannot read the config, so it hands the number to each activity that counts tokens:
`llm_turn`, `tool_exec` and `append_results`. They work out what it changes (settings.Context
.for_window), and a run without one gets the config's window.

F6, a prompt over the window. `llm_turn` refuses a request that its projection puts over the hard
limit, and the provider can reject one too. Both arrive as a provider error of kind
context_too_long. The workflow then compacts with `forced=True`, which keeps the last two turns
and the notes, and asks the turn again, once. It does not ask again when the compaction folded
nothing, because the view would be the same. A second overflow in the same turn, or one with
nothing to fold (the first turn has no view at all), ends the run with "context limit" as before.

The end of a run is noted for the metrics (LH-704). Counting is I/O, so the workflow cannot do it.
When run() ends, by any of the outcomes above or by an activity that failed for good, it calls
the `record_event` local activity once with the type acts.OUTCOME_EVENT, and the worker counts the
task as finished and no longer active. A hand-over to a new run (below) is not an end and makes no
such call. The note is made after the cleanup of a cancel, outside `_call`, because `_call` stops
at once when a cancel was requested. It is a record for people, like the hand-over event: a
failure to write it is logged and the outcome stands. A run that Temporal itself ends, by a
terminate or a timeout, never gets to make it, and its task stays among the active ones until the
worker restarts.

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

Memory (LH-604, ADR-0010). When prepare_workspace says that the worker keeps memory
(`PrepareWorkspaceOutput.memory_enabled`), the workflow calls `load_memory` once, right after it,
with the spec and the repo map that prepare_workspace wrote. The activity returns the ref of the
memory block, the notes about the repo that end the system prompt, and the workflow hands it to
every `llm_turn` and `compact`. The block is loaded once and not on each turn because the prompt
must be the same bytes for the whole task, or the prompt cache of every turn is lost (ADR-0013).
It is read from the recorded result of prepare_workspace, so a history from before this story,
whose result has no flag, replays without the call, and a worker with no store adds no activity to
its tasks. The flag alone is not enough (F13). During a rolling upgrade a new worker can write a
prepare result with the flag on, and the next workflow task can run on an old worker, which ignores
the flag and schedules `llm_turn`. A new worker that replays that history would schedule
`load_memory` in its place, and the replay would fail. So the call is also behind
`workflow.patched(LOAD_MEMORY_PATCH)`: a history that has no marker for it replays without the
call, and every task that loads memory records the marker. Drop the patch with
`workflow.deprecate_patch` once no run that began before this story is open.

A `load_memory` that fails after its retries does not fail the task: the notes are hints, and the
task goes on with a prompt that has no block.

If prepare_workspace found a test command for a repo whose settings name none, it goes into the
spec for the whole task, so every prompt names it.

The memory ref is kept in the state, so it carries over Continue-As-New with the rest of it and
the new run builds its prompts from the same block without calling the activity again. CarryState
is a shared contract that this story does not change, so the workflow takes acts.RunState, which
adds the one field (see there). The JSON of an older run's state decodes into it with no block,
and such a run has never had one.

Continue-As-New (failure mode F12). A run keeps its history short by handing over to a new run of
the same workflow id. After a turn that did not end the run, the workflow hands over when the
turns done are a multiple of the interval (acts.CONTINUE_AS_NEW_EVERY_TURNS, 40, or the run's
Tuning), or when the server suggests it (workflow.info().is_continue_as_new_suggested()). A run
that has used its turns ends with "turn limit" instead, because the new run would only find that
out after a bundle upload. The hand-over goes in this order:

1. `bundle_checkpoint` uploads the history of the checkpoint repo, so that a lost host costs at
   most the work since the last hand-over (failure mode F4);
2. `record_event` writes a `continue_as_new` event (a local activity);
3. the workflow waits for its signal handlers to finish (workflow.all_handlers_finished), but
   not for long: it stops waiting at a cancel request, and after acts.STATUS_GRACE it drops a
   status comment that is still being posted;
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
so none is dropped). The Tuning is in the memo, which continue_as_new carries over, and so is the
count of status comments (`_memo_to_carry`). The new run calls `ensure_sandbox` first, like any
run that starts with a sandbox id (LH-206).

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
        Outcome,
        OutcomeKind,
        Phase,
        Plan,
        ProviderErrorKind,
        Role,
        TaskStatus,
        ToolResult,
    )
    from longhaul.durable import acts
    from longhaul.github import lifecycle
    from longhaul.roles import planner


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
# The summary of the outcome of a task of an issue whose work changes no file.
NOTHING_TO_DELIVER = "nothing to deliver: no file changed"

# The patch marker of the load_memory call (LH-604, failure mode F13). See "Memory" above.
LOAD_MEMORY_PATCH = "lh-604-load-memory"

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


def _error_type(err: FailureError) -> str:
    """The type of the error that failed the run: an application error's type, else its class."""
    cause = err.cause or err
    if isinstance(cause, ApplicationError) and cause.type:
        return cause.type
    return type(cause).__name__


def _is_sandbox_gone(err: ActivityError) -> bool:
    """True when a tool call failed because its container was not there.

    The activity raises that as an ApplicationError of type SANDBOX_GONE, so the cause says it.
    """
    cause = err.cause
    return isinstance(cause, ApplicationError) and cause.type == acts.SANDBOX_GONE


def _has_nothing_to_deliver(err: ActivityError) -> bool:
    """True when build_pr_branch failed because the work changes no file."""
    cause = err.cause
    return isinstance(cause, ApplicationError) and cause.type == acts.NOTHING_TO_DELIVER


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


@workflow.defn(name="TaskWorkflow", failure_exception_types=acts.FAIL_THE_WORKFLOW)
class TaskWorkflow:
    # @workflow.init runs before any signal handler, also for a signal that arrives in the
    # same first workflow task as the start. Building the state in run() lost most of the
    # signals sent while a workflow was starting (ADR-0004, appendix section 4).
    @workflow.init
    def __init__(self, state: acts.RunState) -> None:
        self.state = state
        self.outcome: Outcome | None = None
        self.cancel_request: CancelRequest | None = None
        self.plan_decision: PlanDecision | None = None
        self.idle = 0  # turns in a row that ended without a tool call
        # The status comments posted so far, which a hand-over carries in the memo. None until
        # the first request of this run: only then is the memo read (see _next_status_number).
        self.status_posts: int | None = None

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

    # A task that ends while its status comment is still being posted does not wait for it: the
    # person asked for the state of a task that is over, and a retry is not worth a delay.
    @workflow.signal(unfinished_policy=workflow.HandlerUnfinishedPolicy.ABANDON)
    async def report_status(self, by: str) -> None:
        """Post the state of the task on its issue. `by` is the person who asked."""
        spec = self.state.spec
        if not lifecycle.is_github_task(spec):
            return  # a local task has no issue to answer on
        # The count goes up before the first await, so two requests that arrive together get
        # two keys. The comment is built from the state as it is when the request arrives.
        number = self._next_status_number()
        comment = lifecycle.status_comment(spec, self._snapshot(), by=by, n=number)
        try:
            await workflow.execute_activity(
                acts.POST_COMMENT.name,
                comment,
                result_type=acts.POST_COMMENT.result_type,
                **acts.options(acts.POST_COMMENT, self._tuning()),
            )
        except ActivityError as err:
            # Not _call: a cancel must not be raised from inside a handler, and a status that
            # was asked for is no reason to end the task.
            workflow.logger.warning(
                "status comment %d of task %s not posted: %s",
                number,
                spec.task_id,
                err.cause or err,
            )

    @workflow.query
    def status(self) -> TaskStatus:
        return self._snapshot()

    def _snapshot(self) -> TaskStatus:
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
    async def run(self, state: acts.RunState) -> Outcome:
        # The input is already in self.state. The SDK needs the two signatures to match.
        try:
            outcome = await self._finish_the_task()
        except FailureError as err:
            # The workflow is about to fail. The task is over all the same, and its error type is
            # all the note says: the message of a provider error can echo the request.
            await self._note_outcome(
                Outcome(kind=OutcomeKind.ERROR, summary=f"workflow failed: {_error_type(err)}")
            )
            raise
        await self._note_outcome(outcome)
        return outcome

    async def _finish_the_task(self) -> Outcome:
        try:
            await self._pick_up()
            await self._prepare()
            if self._plans():
                ended = await self._plan()
                if ended is not None:
                    return ended
            outcome = await self._code()
            if self.state.phase is Phase.DELIVERING:
                outcome = await self._deliver(outcome)
            return outcome
        except _CancelRequested:
            return await self._cancelled()

    async def _pick_up(self) -> None:
        """Tell the issue that the task has begun: the first activity of a task of an issue."""
        state = self.state
        if state.sandbox_id is not None or not lifecycle.is_github_task(state.spec):
            return
        state.phase = Phase.PREPARING
        await self._call(acts.POST_COMMENT, lifecycle.pickup_comment(state.spec))
        await self._label(lifecycle.on_pickup(state.spec))

    async def _note_outcome(self, outcome: Outcome) -> None:
        """Tell the worker that the task ended, so that it counts it (see the module docstring)."""
        state = self.state
        note = acts.RecordEventInput(
            task_id=state.spec.task_id,
            type=acts.OUTCOME_EVENT,
            payload={
                "kind": outcome.kind.value,
                "repo": state.spec.repo,
                "turns": state.turn_index,
                "cost_usd": state.cost_usd,
                "summary": outcome.summary[: acts.OUTCOME_SUMMARY_CHARS],
            },
        )
        try:
            await workflow.execute_local_activity(
                acts.RECORD_EVENT.name,
                note,
                result_type=acts.RECORD_EVENT.result_type,
                **acts.local_options(acts.RECORD_EVENT),
            )
        except FailureError as err:
            workflow.logger.warning(
                "outcome of task %s not recorded: %s", state.spec.task_id, err.cause or err
            )

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
        self._keep_base(prepared.base_sha)
        self._use_test_command(prepared.test_command)
        if prepared.memory_enabled and workflow.patched(LOAD_MEMORY_PATCH):
            state.memory_ref = await self._load_memory(prepared.repo_map_ref)

    def _keep_base(self, base_sha: str) -> None:
        """Keep the commit the workspace was prepared at, for the base of the pull request.

        The checkpoint repo cannot say it: the sandbox has the repo read-write and can move its
        `base` tag. The state can, and it reaches the run that delivers, across hand-overs.
        CarryState has no field for it, so it goes on the spec, which is part of the state and
        has the field `base_sha`. Only the task of an issue is delivered, so only it keeps one.
        """
        if lifecycle.is_github_task(self.state.spec):
            self.state.spec = self.state.spec.model_copy(update={"base_sha": base_sha})

    def _use_test_command(self, found: str | None) -> None:
        """Name the verified test command in the spec of a repo whose settings name none.

        The spec is in CarryState, so the command is in every later run and in every prompt of
        the task. A command in the settings is never replaced.
        """
        state = self.state
        if found and not state.spec.settings.test_command:
            settings = state.spec.settings.model_copy(update={"test_command": found})
            state.spec = state.spec.model_copy(update={"settings": settings})

    async def _load_memory(self, repo_map_ref: BlobRef | None) -> BlobRef | None:
        """Write the memory block of the task. None when it could not be written.

        A failure after the retries is logged and the task goes on without notes: they are hints.
        A cancel is not a failure and passes through.
        """
        state = self.state
        try:
            return await self._call(
                acts.LOAD_MEMORY, acts.LoadMemoryInput(spec=state.spec, repo_map_ref=repo_map_ref)
            )
        except ActivityError as err:
            workflow.logger.warning(
                "no memory block for task %s: %s", state.spec.task_id, err.cause or err
            )
            return None

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

    # ---------- the planning phase ----------
    def _plans(self) -> bool:
        """True when the task plans before it codes and has no plan yet."""
        if self.state.plan is not None:
            return False
        return bool(workflow.memo_value(acts.PLAN_MEMO_KEY, False, type_hint=bool))

    async def _plan(self) -> Outcome | None:
        """Run the planner until it submits a plan. Returns None once the plan is in place.

        An outcome means the task ends here: the planner failed its repair turn, used up its
        turns, ran out of the task's turn budget, or was stopped by one of the ends that the coder
        shares (a refusal, a full window, a lost sandbox).
        """
        state = self.state
        state.phase = Phase.PLANNING
        failures = 0  # plans that were not valid so far, for the one repair turn (F14)
        idle = 0  # replies in a row that ended without a tool call
        for _ in range(acts.PLANNER_MAX_TURNS):
            if self._out_of_turns():
                return self._end(rules.turn_limit_outcome(), Phase.FAILED)
            index = state.turn_index
            sampled = await self._sample(index, Role.PLANNER)
            if isinstance(sampled, Outcome):
                state.turn_index = index + 1
                return self._end(sampled, Phase.FAILED)
            calls = rules.runnable_calls(sampled, sampled.tool_call_summaries)
            if not calls:
                verdict = rules.idle_turn(idle)
                idle = verdict.count
                ended = await self._planner_idle(index, sampled, verdict)
                if ended is not None:
                    return ended
                continue

            idle = 0
            ran = await self._planner_calls(index, sampled, calls, failures)
            if isinstance(ran, Outcome):
                return ran  # the sandbox was lost for good
            failures = ran.failures
            if ran.outcome is not None:
                return self._end(ran.outcome, Phase.FAILED)
            if ran.plan is not None:
                await self._accept(ran.plan)
                return None
            await self._relieve(index, sampled.context_decision, Role.PLANNER)
        return self._end(planner.no_plan_outcome(acts.PLANNER_MAX_TURNS), Phase.FAILED)

    async def _planner_calls(
        self,
        index: int,
        reply: acts.LlmTurnOutput,
        calls: list[acts.ToolCallSummary],
        failures: int,
    ) -> planner.PlanCalls | Outcome:
        """Run the calls of one planner turn and answer them all in the transcript.

        Every call is answered, the submission included, so the stored transcript is paired
        whether the planning goes on or ends here. A sandbox that is lost for good ends the run,
        and the outcome is returned.
        """
        state = self.state
        finished: list[ToolResult] = []  # kept for the transcript if a call loses the sandbox

        async def run(call: acts.ToolCallSummary) -> ToolResult:
            result = await self._run_tool(index, call, read_only=True)
            finished.append(result)
            return result

        try:
            ran = await planner.run_calls(calls, run, failures)
        except _SandboxLost as lost:
            return await self._end_lost(index, reply.transcript_ref, calls, finished, lost)
        state.transcript_ref = await self._call(
            acts.APPEND_RESULTS,
            acts.AppendResultsInput(
                task_id=state.spec.task_id,
                turn_index=index,
                transcript_ref=reply.transcript_ref,
                results=[acts.summarize_result(r) for r in ran.results],
                window_tokens=self._window(),
            ),
        )
        state.turn_index = index + 1
        return ran

    async def _planner_idle(
        self, index: int, reply: acts.LlmTurnOutput, verdict: rules.Idle
    ) -> Outcome | None:
        """A planner reply with no tool calls. The first gets a nudge, and a second ends the run."""
        state = self.state
        if verdict.outcome is not None:
            state.turn_index = index + 1
            return self._end(verdict.outcome, Phase.FAILED)
        state.transcript_ref = await self._call(
            acts.APPEND_RESULTS,
            acts.AppendResultsInput(
                task_id=state.spec.task_id,
                turn_index=index,
                transcript_ref=reply.transcript_ref,
                results=[],
                user_text=planner.NUDGE,
                window_tokens=self._window(),
            ),
        )
        state.turn_index = index + 1
        await self._relieve(index, reply.context_decision, Role.PLANNER)
        return None

    async def _accept(self, plan: Plan) -> None:
        """Keep the plan the planner submitted and get ready for the coder.

        The plan is written to the `plans` table and, for a task of an issue, posted on the issue,
        before it is in `state`: `status()` shows a plan version only once the plan is stored and
        out. The coder starts a transcript of its own, with no notes of the planner's.
        """
        state = self.state
        version = state.plan_version + 1
        task_id = state.spec.task_id
        await self._call(
            acts.STORE_PLAN, acts.StorePlanInput(task_id=task_id, version=version, plan=plan)
        )
        issue = state.spec.issue_number
        if issue is not None and lifecycle.is_github_task(state.spec):
            await self._call(
                acts.POST_PLAN,
                acts.PostPlanInput(
                    task_id=task_id, repo=state.spec.repo, number=issue, version=version, plan=plan
                ),
            )
        state.plan = plan
        state.plan_version = version
        state.transcript_ref = None
        state.notes = None

    # ---------- the coding phase ----------
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
        await self._settle_handlers()
        # A cancel that came in during the awaits above must not be lost with this run. It is
        # acted on here. No await follows, so none can arrive between this look and the call.
        self._stop_if_cancelled()
        # The SDK converts the state of a hand-over after this code has let go of it, and an
        # error there does not end the run: the workflow stays open and does nothing. Converting
        # it here raises the guard's error where FAIL_THE_WORKFLOW ends the run with it
        # (ADR-0005). A signal can add to the hints, so the look has to be this late.
        workflow.payload_converter().to_payload(state)
        workflow.continue_as_new(state, memo=self._memo_to_carry())

    async def _settle_handlers(self) -> None:
        """Give a status comment that is being posted a short while to finish before a hand-over.

        The only handler that awaits is report_status, and its activity retries for about twenty
        minutes when GitHub fails. Waiting for it without a limit would stop the task for that
        long, and a cancel that came in meanwhile would wait with it. So the wait ends at the
        first of: every handler is done, a cancel request, or the grace (acts.STATUS_GRACE). In
        the last case the comment is dropped with this run. Its number is in the memo, so no
        later comment reuses its key.
        """
        if workflow.all_handlers_finished():
            return  # the usual case, and it needs no timer in the history
        try:
            await workflow.wait_condition(
                lambda: workflow.all_handlers_finished() or self.cancel_request is not None,
                timeout=acts.status_grace(self._tuning()),
            )
        except TimeoutError:
            workflow.logger.warning(
                "task %s hands over with a status comment still being posted: it is dropped",
                self.state.spec.task_id,
            )

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
                window_tokens=self._window(),
            ),
        )
        state.turn_index = index + 1
        if ran.finish is not None:
            return self._finish(rules.finished(ran.finish))
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
                window_tokens=self._window(),
            ),
        )
        state.turn_index = index + 1
        return self._end(rules.error(SANDBOX_LOST), Phase.FAILED)

    async def _sample(self, index: int, role: Role = Role.CODER) -> acts.LlmTurnOutput | Outcome:
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
                        role=role,
                        spec=state.spec,
                        transcript_ref=state.transcript_ref,
                        notes=state.notes,
                        plan=state.plan,
                        memory_ref=state.memory_ref,
                        hints=list(state.hints),
                        high_output_cap=sampling.retried,
                        paused_ref=paused_ref,
                        script_ref=self._script(),
                        window_tokens=self._window(),
                        context_mode=self._context_mode(),
                    ),
                )
            except ActivityError as err:
                if (
                    _provider_error_kind(err) is ProviderErrorKind.CONTEXT_TOO_LONG
                    and not compacted
                ):
                    compacted = True
                    if await self._compact(index, forced=True, role=role):
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
    async def _relieve(self, index: int, decision: Decision, role: Role = Role.CODER) -> None:
        """Compact the view when the model turn that just ended said the next request needs it.

        This runs when the turn is over and its results are in the view, so a hand-over that
        follows carries the compacted view and its notes.
        """
        if decision != "ok":
            await self._compact(index, forced=decision == "force", role=role)

    async def _compact(self, index: int, *, forced: bool, role: Role = Role.CODER) -> bool:
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
                role=role,
                spec=state.spec,
                transcript_ref=state.transcript_ref,
                notes=state.notes,
                forced=forced,
                script_ref=self._script(),
                context_mode=mode,
                memory_ref=state.memory_ref,
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
                window_tokens=self._window(),
            ),
        )
        state.turn_index = index + 1
        await self._relieve(index, reply.context_decision)
        return None

    async def _run_tool(
        self, index: int, call: acts.ToolCallSummary, *, read_only: bool = False
    ) -> ToolResult:
        """Run one tool call. `read_only` is for the planning phase: the tree is put back after."""
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
                        read_only=read_only,
                        window_tokens=self._window(),
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

    # ---------- delivery and labels ----------
    def _finish(self, finished: Outcome) -> Outcome:
        """The model finished. A local task is done. The task of an issue still has to deliver."""
        if lifecycle.is_github_task(self.state.spec):
            self.state.phase = Phase.DELIVERING  # run() delivers, and ends the task
            return finished
        return self._end(finished, Phase.DONE)

    async def _deliver(self, finished: Outcome) -> Outcome:
        """Open the draft pull request and move the label. Ends the task with its URL."""
        state = self.state
        issue = state.spec.issue_number
        base_sha = state.spec.base_sha
        if state.head_sha is None or base_sha is None or issue is None:
            # All three are set for a task of an issue that has run. Asking again changes nothing.
            raise ApplicationError(
                "a pull request cannot be built: the task has no base, no checkpoint or no issue",
                non_retryable=True,
            )
        task_id, repo = state.spec.task_id, state.spec.repo
        try:
            built = await self._call(
                acts.BUILD_PR_BRANCH,
                acts.BuildPrBranchInput(
                    task_id=task_id,
                    repo=repo,
                    summary=finished.summary,
                    base_sha=base_sha,
                    head_sha=state.head_sha,
                ),
            )
        except ActivityError as err:
            if not _has_nothing_to_deliver(err):
                raise
            return self._end(rules.error(NOTHING_TO_DELIVER), Phase.FAILED)
        pr_url = await self._call(
            acts.DELIVER,
            acts.DeliverInput(
                task_id=task_id,
                repo=repo,
                issue_number=issue,
                summary=finished.summary,
                branch=built.branch,
                base_branch=built.base_branch,
                base_sha=built.base_sha,
                head_sha=built.head_sha,
                title=built.title,
            ),
        )
        await self._label(lifecycle.on_pr_opened(state.spec))
        done = Outcome(kind=OutcomeKind.PR_OPENED, pr_url=pr_url, summary=finished.summary)
        return self._end(done, Phase.DONE)

    async def _label(self, steps: list[lifecycle.LabelStep]) -> None:
        """Run label changes in order, each as an idempotent activity."""
        for step in steps:
            await self._call(acts.ADD_LABEL if step.add else acts.REMOVE_LABEL, step.label)

    async def _needs_human(self, pause: int) -> None:
        """Ask for a person on the issue. For the pause of Phases 5 and 7: `pause` counts from 1."""
        if lifecycle.is_github_task(self.state.spec):
            await self._label(lifecycle.on_pause(self.state.spec, pause))

    async def _human_done(self, pause: int) -> None:
        """Take the request for a person back when the task resumes."""
        if lifecycle.is_github_task(self.state.spec):
            await self._label(lifecycle.on_resume(self.state.spec, pause))

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

    def _next_status_number(self) -> int:
        """The number of the next status comment: the count carried in the memo, plus one.

        The memo is read on the first request of a run and not in __init__, so a workflow that
        is built outside a Temporal loop, as the signal unit tests do, still works.
        """
        if self.status_posts is None:
            self.status_posts = workflow.memo_value(acts.STATUS_MEMO_KEY, 0, type_hint=int)
        self.status_posts += 1
        return self.status_posts

    def _memo_to_carry(self) -> dict[str, Any] | None:
        """The memo of the next run: this one, plus the count of status comments if it changed.

        None keeps the memo as it is, which already holds the count of an earlier run when this
        run has had no status request. A memo that is passed replaces the whole memo, so it
        starts from the current one.
        """
        if self.status_posts is None:
            return None
        return {**workflow.memo(), acts.STATUS_MEMO_KEY: self.status_posts}

    def _tuning(self) -> acts.Tuning | None:
        return workflow.memo_value(acts.TUNING_MEMO_KEY, None, type_hint=acts.Tuning)

    def _script(self) -> BlobRef | None:
        return workflow.memo_value(acts.SCRIPT_MEMO_KEY, None, type_hint=BlobRef)

    def _window(self) -> int | None:
        """The context window the run asked for, or None when the config of the worker decides."""
        tuning = self._tuning()
        return tuning.window_tokens if tuning is not None else None

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
