"""TaskWorkflow: the one durable owner of a task.

It holds the task's CarryState, answers `status()`, takes the signals that steer a task, and
drives the phases through activities: PREPARING, PLANNING when the task asks for it, a CODING loop
that goes through VERIFYING when the task has a plan, then DONE. The task of an issue goes through
DELIVERING before DONE (see "A task of an issue" below). All I/O happens in activities, which this
module calls by name through longhaul.durable.acts. It imports no I/O library (invariant I1,
checked by tests/unit/test_invariants.py), reads time only through workflow.now() (it needs none
yet), and uses no random numbers.

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

The plan gate (LH-502). A repo whose settings have `plan_approval_required` holds the plan after
step 3 until a person has answered, and a repo without it goes on to CODING as before. The gate
applies to a task that plans: a task without the plan switch has no plan to hold.

1. Phase AWAITING_PLAN_APPROVAL. A task of an issue adds `longhaul:needs-human` and comments how
   to answer (github/lifecycle.py, keys with the version of the plan), and the workflow waits for
   `approve_plan`, `reject_plan`, a cancel, or acts.HINT_WAIT_HOURS (24, the config's
   `stuck.hint_wait_hours`). A task on a folder waits the same way and asks nobody.
2. `approve_plan` ends the wait. The `decide_plan` activity records the status and the login in
   the `plans` table (the workflow does not write to the database), the label comes off, and the
   coder starts from this version.
3. `reject_plan(by, feedback)` records the rejection and takes the label off, then sends the
   planner back for the next version. The feedback reaches the planner as a hint from the
   person, so it is a user message in the planner's own transcript, which the planner keeps: the
   revision starts from what it had read. It goes in `self.revision_hints` and not in
   `state.hints`, so the coder, which has a transcript of its own, never sees it. The new version
   is stored and posted like the first, and put to the same wait. Each version has its own
   allowance of acts.PLANNER_MAX_TURNS planner turns and one repair turn (F14). The planner is
   asked for at most planner.MAX_PLAN_VERSIONS (3) versions. The third rejection ends the task
   as STUCK "plan not approved", and so does a wait that runs out, as STUCK "plan not approved:
   no decision in 24 hours". On the timeout the label stays, because a person is still needed.
4. A decision that arrives before the wait is not lost. The signal handlers keep the last
   decision in `self.plan_decision`, and the gate takes it when it asks for one, without waiting.
   That covers an approval sent while the planner works, and one sent while it revises. Such a
   decision is taken as it is: the issue gets no request for a decision and no label goes on or
   off. The gate sets `state.plan_approved` when it takes the decision, and the handlers do not.
   The gate clears the decision when it takes it, so a decision applies to one version, and an
   approval or a rejection that arrives after the coder started finds nobody to read it. Two
   decisions that arrive in the same workflow task are one: the later one counts. The poller sends
   a command a second time only after a crash between the signal and its cursor, and a repeated
   rejection then costs one more revision.
5. Replay (F13). The gate is behind `workflow.patched(PLAN_GATE_PATCH)`, asked only when the
   repo's setting is on, so a history without the gate has no marker and a history from before
   this story replays without it. The marker is in every history that passes the gate. Drop the
   patch with `workflow.deprecate_patch` once no run that began before this story is open. The
   wait and the revision loop issue the same commands in the same order on every replay: the
   decision is read from the state that the signals changed, never from a clock or a lookup.

The coder with a plan (LH-503). A task that has a plan codes differently, and a task that has none
codes as it always did. The coder of a planned task is offered `update_step`, `submit_for_review`
and `ask_human` in place of `finish`, and reads the plan in the first message of its transcript
(roles/coder.py, agent/prompts.py). The workflow handles those three calls itself, as it does
`finish`:

1. `update_step` goes to the `record_step` activity, which writes the event and, for a step that is
   done, a `step` checkpoint at `state.head_sha`. Only then does the new status go into
   `state.plan`, so `status()` never shows a step as done before its checkpoint is saved. The
   next `llm_turn` gets the plan in its input and brings the first message up to date. A call
   that names a step the plan does not have, or a status it does not know, is answered as an
   error and changes nothing.
2. `submit_for_review` ends the coding and starts the review (see "The verifying phase" below). A
   call to `finish` still ends such a task the way it did before the verifier, without a review,
   for a worker or a history from before that story. The `build_pr_branch` activity gets the
   plan, and makes one commit for each done step.
3. `ask_human` pauses the task for a person, as a stuck signal does (see "The pause for a hint"
   below).

`status()` of a task with a plan answers with `acts.PlannedStatus`: the TaskStatus and the plan
with the status of each step. TaskStatus is a contract and has no field for the plan. A client
that wants the plan asks by name: `handle.query("status", result_type=acts.PlannedStatus)`.

The verifying phase (LH-504, ADR-0007). A task that has a plan does not trust `submit_for_review`.
The phase becomes VERIFYING and the work is checked in two halves, and it passes only when both
say so (roles/verifier.py):

1. The pre-check. `tool_exec` runs the test command of the repo and every acceptance check of the
   plan that is a command, then `git_diff`, all marked read-only, so a file that a command writes
   is put back. A command that failed fails the review, whatever the verifier says.
2. The verdict. A verifier with a fresh transcript, a tool list and a prompt of its own gets the
   plan, the pre-check and the diff in its first message, and ends by calling `submit_verdict`
   with a `VerifierVerdict`. The workflow judges the call itself, as it does `submit_plan`: an
   invalid verdict gets one repair turn (F14), and a second one ends the task with ERROR
   "verifier submitted an invalid verdict twice: ...". A verifier that uses its turns without a
   verdict ends it with ERROR "verifier submitted no verdict in 15 turns". Neither is a pass.

A review that passes ends the coding as `finish` did: a task of an issue goes on to DELIVERING,
and the pull request is opened ready for review, with "Verified: <tests_summary>" in its body. A
review whose pre-check ran no command (no test command for the repo, none in the plan) can still
pass on the verdict alone, but then the counts are the model's word, so the pull request is a draft
and does not say "Verified". A review that fails adds a round to `state.verify_rounds`, which
crosses hand-overs. Below acts.MAX_VERIFY_ROUNDS the coder is sent back: a `verdict` event records
the round, the coder's transcript and notes (set aside while the verifier had the view) are put
back, and a user message "Review failed: <issues>. Fix step <id>." goes at the end of that
transcript. The phase is CODING again. At acts.MAX_VERIFY_ROUNDS the task ends as STUCK, in phase
FAILED, with the reason and the last review's issues in the summary.

Replay. The new activity calls are on the paths a plan opens: `record_step` runs only when the
coder of a planned task calls `update_step`, and the extra input of `build_pr_branch` is the plan.
The review adds `tool_exec` calls for the pre-check and the diff, and the verifier's own turns, and
makes them only after a `submit_for_review`. A task without a plan makes the commands it made
before, in the same order, and so does a planned task that ends on `finish`, so those histories
replay as they were. A planned task that submitted for review before this story replays
differently, which matters only for a deployment that has such a history in flight (F13).

The stuck detector (LH-505). After each coder turn that does not end the run, `_watch` shows the
turn to the detector of roles/stuck.py. The detector keeps its window in `state.stuck_window`, so
it crosses a hand-over. Its facts come from two places: tool_exec returns a `CallSignal` with each
result (acts.ToolExecResult), and the workflow adds what only it knows, the steps done and which
calls failed. When a rule fires, the workflow records one `stuck` event with the reason and the
evidence, and `_watch` returns the signal for the pause to act on. The planner is not watched:
its calls get no facts, and the detector is told the phase besides. The thresholds are the
`stuck` section of the config, copied into roles/stuck.py because the workflow cannot read the
config file, and a worker whose config differs stops at the start. Replay: the event is the one
command this adds, and only on a turn where a rule fires, so a history in which none does replays
as it was.

The pause for a hint (LH-506, ADR-0007). A stuck signal from `_watch`, a good `ask_human` call, or
a call that a Keystone gateway holds for approval (LH-909, `ToolExecResult.approval_question`),
pauses the task in `_pause`:

1. The phase becomes PAUSED. The workflow notes how many hints and budget top-ups it has by now,
   before anything else is awaited, so that a signal that arrives while the question is written
   counts as an answer to it. A hint that came earlier answered nothing yet.
2. `summarize_for_human`, a call to the compactor model, writes what the agent is trying to do,
   what it tried, its guess and one question, and renders them as the comment of
   templates/stuck_comment.md.j2. Its cost is added to the task. A task of an issue then posts
   the comment under the key `stuck:<n>` and adds `longhaul:needs-human`, through the side-effect
   activities. A local task has no issue, so the comment goes into the `paused` event.
3. The workflow waits for a `hint`, an `add_budget` or a `cancel`, up to `stuck.hint_wait_hours`
   (acts.HINT_WAIT_HOURS, a copy of the config value like the detector's limits).

A hint or a top-up resumes the task: the detector's window is emptied, `needs-human` is taken
off, a `resumed` event is recorded, `state.stuck_count` goes up by one and the phase is CODING
again. The hint is shown to the model on the next turn like any other (turns.show_hints), as
"Hint from <user>: <text>" after the results of the turn that paused. `stuck_count` is the number
of pauses that have ended, so the next pause is `stuck_count + 1`, which is the `n` of the key and
survives a hand-over in CarryState. A task that is stuck after its third pause (stuck.MAX_PAUSES),
and a task whose wait runs out, end with outcome STUCK in phase FAILED. A cancel during the wait
takes the usual cancel path. Those ends get the final comment and labels of every end (see "How a
task ends" below).

Each hint is cut to agent/hints.MAX_HINT_BYTES when its signal arrives. The hints of a task stay
in the state for its whole life, and every `llm_turn` takes all of them as input.

Replay. The pause is behind `workflow.patched(PAUSE_PATCH)`. A history from before this story
has no marker and replays as it ran: a stuck signal was only recorded, and `ask_human` recorded
a `paused` event and ended the task as stuck in phase PAUSED. Drop the patch with
`workflow.deprecate_patch` once no run that began before this story is open.

Budgets (LH-702). A task started with `enforce_budgets` (the poller starts every task so) checks
its limits before each model turn, in `_afford`: a turn of the planner, of the coder and of the
verifier alike. A task without the switch makes no check and ends at its turn limit with ERROR
"turn limit", as every task did before.

1. The wall clock is judged here, from `workflow.now()`, `state.started_at` and the time the task
   spent in pauses (`state.paused_s`): a person who answers after three hours has not used three
   hours of the task. The wait for a decision on a plan (LH-502) counts as a pause. The other
   limits are judged by the `check_budget` activity, which reads the ledger. It is given the turn
   index, the top-ups, the running cost, the size of the last request and `workflow.now()`, so the
   workflow reads no database and no clock of its own.
2. A limit that would be crossed pauses the task in `_budget_pause`. The phase becomes PAUSED,
   `post_budget` writes the comment under the key `budget:<n>` (a task of an issue), the label
   `longhaul:needs-human` goes on, and a `paused` event records the reason and the numbers. A
   local task has no issue, so the event is all it has. `n` is `state.budget_pauses`, which
   crosses a hand-over.
3. A limit of the task (dollars, turns, minutes) waits for `add_budget` or `cancel` up to
   `stuck.hint_wait_hours`. A top-up resumes the task with a `resumed` event and a `budget_added`
   event, and the next check uses the new limits. One that is too small pauses the task again,
   with the next number. A wait that runs out ends the task with outcome BUDGET_EXHAUSTED. A
   `hint` does not answer a budget pause: money is what the task waits for. After a wall-clock
   pause a top-up of any amount starts a new window of the same length, because `add_budget`
   has no minutes, and the task would otherwise stop again at once.
4. The daily limit of the repo is the one a person cannot lift. The task sleeps on a durable
   timer until 00:00 UTC, the comment says so and no label asks for anyone, and then it checks
   again with no signal. A call that is bigger than a whole day of the repo's budget would find
   the same answer tomorrow, so the task ends with BUDGET_EXHAUSTED at once.
5. No `llm_turn` is scheduled while the task waits: the check comes before the call, and the wait
   is inside the check.
6. A budget that ends the task ends it like any other end: the outcome goes through `_wind_up`,
   with the cleanup, the final comment, the labels and the lessons.

Replay. The check and the pause are behind `state.enforce_budgets`, which is part of the state
the run started with. A history from before this story has no such field, decodes it as False and
replays as it ran. A task that enforces budgets adds commands only where it checks or pauses.

A task of an issue (LH-306) is one whose repo is owner/name and whose spec has an issue number
(lifecycle.is_github_task). It differs from a local task in three places and nowhere else:

1. Its first activity is the pickup: the "Picked up" comment and the `longhaul:working` label on
   the issue, both written through github/side_effects.py with stable keys. A run that starts
   with a sandbox id, because an earlier run prepared it and handed over, has been picked up
   already and does not repeat it. The keys would make a repeat harmless, but each one costs an
   activity and a place in the history.
2. When the model calls `finish`, or the work passes its review, the phase becomes DELIVERING and
   the task is not over. The `build_pr_branch` activity makes the pull request branch on the base
   the workspace was prepared at (kept in `state.spec.base_sha`, because the `base` tag in the
   checkpoint repo is the agent's to move), and `deliver` pushes it and opens the pull request:
   ready for review when a verifier passed the work and a command backed it, else a draft. The
   task then swaps the label (`longhaul:working` out, `longhaul:pr-open` in) and ends with
   PR_OPENED carrying the URL. `status()` shows no outcome until then.
3. A task whose work changes no file ends with ERROR "nothing to deliver: no file changed",
   because GitHub refuses a pull request with no commit.

The pause for a hint adds and removes `longhaul:needs-human` through `_needs_human` and
`_human_done`, and the pause for a budget (Phase 7) will use them too. The plan gate asks for a
person with helpers and keys of its own (`_ask_for_review`). The labels of a task that ends any
other way are set when it ends (see "How a task ends" below).

`/longhaul status` on the issue reaches the task as the signal `report_status` (LH-308). The
handler posts a comment with the state of the task through the `post_comment` activity, so it is
a side effect with a key like any other, `status:<n>`, where n counts the requests the task has had.
The count is part of the workflow's state, so a replay posts nothing again, and the memo carries it
over a Continue-As-New. The handler is best effort: a status that cannot be posted is logged and
does not fail the task, and a cancel does not wait for it, not in a turn and not at a hand-over
(see `_settle_handlers`). It posts for a task of an issue only.

How a run ends. `finish` is the one good end. Every other end has a fixed summary, so a caller
can tell them apart without reading prose:

- the model calls `finish` with a summary, or the work of a coder with a plan passes its review:
  PR_OPENED with the model's summary. A local task has no pull request and so no pr_url, which is
  the outcome the in-process loop returns too. The task of an issue has the URL of its pull
  request;
- a `cancel` signal: CANCELLED;
- `task_turns` plus the turns added by `add_budget` pass: ERROR "turn limit";
- a tool call finds its sandbox gone more often than it may: ERROR "sandbox lost";
- the other ends of agent/rules.py: "no tool calls", "output limit", "model declined (...)",
  "context limit", "pause limit" and "unexpected stop reason: ...".

The planner adds two ends of its own: ERROR "planner submitted an invalid plan twice: ..." and
ERROR "planner submitted no plan in 20 turns". The plan gate adds two more, both STUCK: "plan not
approved" after the third rejection and "plan not approved: no decision in 24 hours" after a wait
that ran out. The verifier adds three: ERROR "verifier submitted an invalid verdict twice: ...",
ERROR "verifier submitted no verdict in 15 turns", and STUCK "the work failed review 3 times; last
review: ..." for a task whose rounds ran out.

The pause for a hint adds two: STUCK with the reason, the evidence or the question, and "no hint in
24 hours" or "3 hints did not unblock the task" (roles/stuck.py).

The budgets add one: BUDGET_EXHAUSTED, with the summary of budget.exhausted_outcome.

An activity that fails for good, after its retries, ends the task with ERROR and then fails the
workflow with that error (see "How a task ends" below). A prompt over the context window is the
exception, and it is handled as described next.

How a task ends (LH-507). Every task ends in exactly one Outcome, and `run()` winds it up the same
way for every end, in `_wind_up`:

1. The Outcome is set, with the phase (`_end`). `status()` shows it from here.
2. The `cleanup` activity destroys the sandbox. This is first because it frees the most and needs
   nothing from GitHub, which can be down for twenty minutes. A task that ended on its own keeps
   its bare repo (`CleanupInput.keep_repo`), and the janitor (`clean_repos`, which PollerWorkflow
   runs after each poll) removes it seven days after the `outcome` event. A cancel removes the
   repo as it always did. For a task of an issue that happens at the end of the wind-up, with a
   second `cleanup`, because the final comment reads the diff from the repo. The reason of both
   is the reason of the cancel.
3. A task of an issue goes on. A stuck task whose repo asked for it (the memo key
   acts.OPEN_DRAFT_MEMO_KEY, set by the poller from `open_draft_on_stuck`) builds the pull request
   branch and opens a draft with its partial work, through the same two activities as a finished
   task. A task whose work changes no file opens none, and a draft that fails is logged. The
   URL becomes the `pr_url` of the outcome.
4. `post_final` posts the one final comment under the key `final:1`: the pull request link, or
   the report with what was done, what is left and the diff stats (github/final_comment.py).
5. The final labels are set (lifecycle.on_end). A task that delivered has them already from
   `_deliver`.
6. A task that keeps memory asks for its lessons (LH-605): `extract_lessons` drafts up to 5 notes
   from the plan, the last progress notes, the stuck events and the verifier's issues, and stores
   them as candidates linked to the pull request number, or to the task id when there is no pull
   request. The workflow records what came back in a `lessons` event, for the run report, and adds
   the cost of the call to the task. It runs on every ending path, a cancel and an error too, and
   after the final comment and the labels, so nobody waits for a model. A task whose worker has no
   memory store has no `memory_ref` and never asks. A failure here is logged and changes nothing
   about the outcome.
7. The `outcome` event is recorded, as before (LH-704), so it counts a task that has done all
   of the above.

Each step is best effort and runs outside `_call`, so a failed step is logged and the next one
goes on, and a cancel that arrives during the wind-up changes nothing. The steps are idempotent:
the comment, the labels and the pull request have keys, a cleanup of nothing is a no-op, and a
replay makes the same commands from the history.

An error ends the task too. An activity that failed for good (a FailureError) or a payload that
was too large (acts.FAIL_THE_WORKFLOW) sets ERROR "workflow failed: <type>", and any other
exception that escapes the run, a bug in the workflow, sets ERROR "unexpected error: <class>". The
summary is the type and never the message, which can echo a request. The trace, cut to
acts.ERROR_TRACE_CHARS, goes to the `events` table as an `error` event (wind_up.error_trace), and
the wind-up runs. Then the workflow still fails, so Temporal shows it as failed and a client that
waits for the result gets the error. A bug that used to fail the workflow task, and so to be
retried for as long as the code stayed the same, now fails the workflow for good.

Replay. All of it is behind `workflow.patched(OUTCOME_PATCH)`. A history from before this story
has no marker and replays as it ran: a cancel cleaned up inside `_cancelled`, no other end cleaned
up, and a failure noted the outcome and raised. Drop the patch with `workflow.deprecate_patch`
once no run that began before this story is open. The lessons are behind
`workflow.patched(LESSONS_PATCH)`, asked only by a task that has a `memory_ref`, so a history that
ended before LH-605 replays without the call, and a task with no memory records no marker.

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
such call. The note is made at the end of the wind-up, outside `_call`, because `_call` stops
at once when a cancel was requested. It is a record for people, like the hand-over event: a
failure to write it is logged and the outcome stands. A run that Temporal itself ends, by a
terminate or a timeout, never gets to make it, and its task stays among the active ones until the
worker restarts.

The context modes (LH-406). `context.mode` of the worker's config, or the mode a run asks for with
`longhaul run --context-mode`, switches the tiers off for an ablation: `no_compaction` keeps tier 1
and `none` has neither (longhaul.context.modes). `sliding`, the window of the naive baseline
(LH-903), has neither tier either, and the workflow never sees it as a refusal: `llm_turn` cuts the
oldest turns before it measures the request. The workflow cannot read the config, so a run's own
mode comes from the memo (acts.CONTEXT_MODE_MEMO_KEY). The workflow hands it to every `llm_turn`
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
- `plan_decision` and `revision_hints` are not carried. The plan gate waits, reads the decision and
  revises inside one run, before any hand-over: a run hands over only between coder turns. The
  approval that remains is `plan_approved` in CarryState.
- `outcome` is never set at a hand-over, because a run that has an outcome ends.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, NoReturn, Protocol

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, FailureError, TemporalError

# Contracts, acts and the turn rules are pure. Passing them through the sandbox saves about
# 30 ms per run and keeps one class object for each model, so an isinstance check against a
# model that came from outside still works (ADR-0004, appendix section 1).
with workflow.unsafe.imports_passed_through():
    from longhaul.agent import hints, rules
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
        StuckReason,
        StuckSignal,
        TaskStatus,
        ToolResult,
        VerifierVerdict,
    )
    from longhaul.cost import budget
    from longhaul.durable import acts, wind_up
    from longhaul.github import lifecycle
    from longhaul.roles import coder, planner, stuck, verifier


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
# The patch marker of the plan gate (LH-502, failure mode F13). See "The plan gate" above.
PLAN_GATE_PATCH = "lh-502-plan-gate"
# The patch marker of the wind-up that ends every task (LH-507, failure mode F13). See "How a task
# ends" above.
OUTCOME_PATCH = "lh-507-final-outcome"
# The patch marker of the extract_lessons call at the end of a task (LH-605). See "How a task
# ends" above.
LESSONS_PATCH = "lh-605-lessons"
# What fails the workflow when it escapes run(): an activity or child that failed for good, and
# the errors the workflow was told to fail on (acts.FAIL_THE_WORKFLOW).
FAILS_THE_WORKFLOW: tuple[type[BaseException], ...] = (FailureError, *acts.FAIL_THE_WORKFLOW)
# The type of the error that fails a workflow whose code raised something it did not expect.
UNEXPECTED_ERROR = "UnexpectedError"

# The event a compaction records (plan section 3.5).
COMPACTION_EVENT = "compaction"

# The events of a pause (plan section 3.5). `paused` has the `reason` (a StuckReason value), the
# `pause` number, the `question`, the `turn_index` and, for a local task, the `comment`. `resumed`
# has the `pause` number, `via` ("hint" or "budget") and, for a hint, who gave it.
PAUSED_EVENT = "paused"
RESUMED_EVENT = "resumed"
ASKED_REASON = StuckReason.AGENT_ASKED.value
HINT_VIA = "hint"
BUDGET_VIA = "budget"

# The events of a budget pause. It records the `paused` and `resumed` events of a pause for a hint,
# with the limit that stopped the task as the `reason` and the `numbers` behind it; the `resumed`
# event says `via` "budget" for a top-up and "midnight" for the daily limit. A top-up also records
# `budget_added` with the dollars and turns that came in since the pause began (plan section 3.5).
BUDGET_ADDED_EVENT = "budget_added"
MIDNIGHT_VIA = "midnight"

# The patch marker of the pause for a hint (LH-506). See "The pause for a hint" above.
PAUSE_PATCH = "lh-506-pause-for-hint"

# The event a stuck detection records (plan section 3.5). The payload has the `reason`, which the
# metrics count (telemetry/metrics.py), the `evidence` lines and the `turn_index`.
STUCK_EVENT = "stuck"

# The event that a review records (plan section 3.5), one for each round.
VERDICT_EVENT = "verdict"

# The event a hand-over to a new run records (plan section 3.5), and the two reasons it gives.
CONTINUE_AS_NEW_EVENT = "continue_as_new"
HAND_OVER_TURNS = "turns"  # the turn count reached the interval
HAND_OVER_HISTORY = "history"  # the server suggested it, because the history grew


@dataclass(frozen=True)
class _Ran:
    """What the calls of a coder turn came to, for a task with a plan or without.

    `finish` is the summary of a good `finish` or `submit_for_review`, and `question` the text of
    a good `ask_human`. Either ends the turn. `review` is true when the summary came with
    `submit_for_review`, so that the work is verified before it is delivered.
    """

    results: list[ToolResult]
    finish: str | None
    question: str | None = None
    review: bool = False


def _held_for_approval(results: Sequence[ToolResult]) -> str | None:
    """The question of the first call in `results` that a gateway holds for a person, if any."""
    for result in results:
        if isinstance(result, acts.ToolExecResult) and result.approval_question is not None:
            return result.approval_question
    return None


class _Answered(Protocol):
    """What the calls of a read-only turn came to, as far as the transcript is concerned."""

    @property
    def results(self) -> list[ToolResult]: ...


@dataclass(frozen=True)
class _Unanswered:
    """What the task had when it paused, so that only a later signal counts as the answer."""

    hints: int
    usd: float
    turns: int


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


def _error_type(err: BaseException) -> str:
    """The type of the error that failed the run: an application error's type, else its class."""
    cause = (err.cause if isinstance(err, FailureError) else None) or err
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
        # The last decision a person sent about the plan, until the gate takes it (LH-502).
        self.plan_decision: PlanDecision | None = None
        # Who sent the last add_budget, for the `budget_added` event of a budget pause (LH-702)
        # and the run report. A pause does not cross a hand-over, so it is not carried.
        self.topped_up_by: str | None = None
        # What the planner is told about rejected versions of its plan. Not carried: planning
        # ends before any hand-over.
        self.revision_hints: list[str] = []
        self.idle = 0  # turns in a row that ended without a tool call
        # The status comments posted so far, which a hand-over carries in the memo. None until
        # the first request of this run: only then is the memo read (see _next_status_number).
        self.status_posts: int | None = None
        # The counts a verifier gave when it passed the work, for the pull request. Delivery
        # follows the review in the same run, so a hand-over never has to carry it.
        self.verified_tests: str | None = None
        # Why the task waits for a person, for the status. None unless it is paused.
        self.paused_reason: str | None = None

    # ---------- signals and the query ----------
    @workflow.signal
    def cancel(self, by: str, reason: str) -> None:
        # The first request wins, so a second cancel cannot change who is named in the outcome.
        if self.cancel_request is None:
            self.cancel_request = CancelRequest(by, reason)

    @workflow.signal
    def hint(self, by: str, text: str) -> None:
        # Cut here, in the handler, because the hints stay in the state for the whole task and
        # every llm_turn takes all of them as input (agent/hints.py).
        self.state.hints.append(f"{hints.clip_author(by)}: {hints.clip_hint(text)}")

    @workflow.signal
    def add_budget(self, by: str, usd: float, turns: int) -> None:
        # A negative amount would lower a budget the task already has. Each amount is judged
        # on its own, so the valid half of a mixed top-up still counts.
        self.state.extra_budget_usd += max(usd, 0.0)
        self.state.extra_turns += max(turns, 0)
        self.topped_up_by = by

    @workflow.signal
    def approve_plan(self, by: str) -> None:
        # state.plan_approved is the gate's to set (_review): a late answer, after the coder has
        # started, must not change what the hand-overs carry.
        self.plan_decision = PlanDecision(approved=True, by=by)

    @workflow.signal
    def reject_plan(self, by: str, feedback: str) -> None:
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
        status = TaskStatus(
            task_id=state.spec.task_id,
            phase=state.phase,
            turn_index=state.turn_index,
            cost_usd=state.cost_usd,
            last_checkpoint=state.head_sha,
            # CarryState counts from 0 for "no plan yet", TaskStatus says None.
            plan_version=state.plan_version or None,
            paused_reason=self.paused_reason,
            outcome=self.outcome,
        )
        if state.plan is None:
            return status
        return acts.PlannedStatus(**status.model_dump(), plan=state.plan)

    # ---------- the run ----------
    @workflow.run
    async def run(self, state: acts.RunState) -> Outcome:
        # The input is already in self.state. The SDK needs the two signatures to match.
        try:
            outcome = await self._finish_the_task()
        except FAILS_THE_WORKFLOW as err:
            # The workflow is about to fail. The task is over all the same, and its error type is
            # all the summary says: the message of a provider error can echo the request.
            await self._end_with_error(err, unexpected=False)
            raise
        except TemporalError:
            raise  # a non-determinism error or an eviction: the SDK handles these
        except Exception as err:
            # A bug in the workflow or in a helper it calls. Before LH-507 it failed the workflow
            # task, which the server retries for as long as the code is the same.
            if not workflow.patched(OUTCOME_PATCH):
                raise
            ended = await self._end_with_error(err, unexpected=True)
            raise ApplicationError(
                ended.summary, type=UNEXPECTED_ERROR, non_retryable=True
            ) from err
        return await self._wind_up(outcome)

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

    # ---------- the end of a task ----------
    async def _end_with_error(self, err: BaseException, *, unexpected: bool) -> Outcome:
        """End the task with outcome ERROR for an exception, and wind it up. The caller raises.

        The summary names the error type and nothing else. The trace goes to the `events` table.
        """
        label = "unexpected error" if unexpected else "workflow failed"
        failed = Outcome(kind=OutcomeKind.ERROR, summary=f"{label}: {_error_type(err)}")
        if not workflow.patched(OUTCOME_PATCH):
            await self._note_outcome(failed)
            return failed
        self._end(failed, Phase.FAILED)
        await self._wind_note(
            acts.ERROR_EVENT, {"type": _error_type(err), "trace": wind_up.error_trace(err)}
        )
        return await self._wind_up(failed)

    async def _wind_up(self, outcome: Outcome) -> Outcome:
        """Everything a task does when it ends, whatever way it ended (LH-507). Returns the outcome.

        The order is the order of what costs the most when it is left behind. The sandbox goes
        first, because GitHub can be down for twenty minutes and a container must not wait for it.
        Then, for a task of an issue: the draft pull request of a stuck task whose repo asked
        for one, the final comment (which links that pull request), and the final labels. The
        repo of a cancelled task goes after them, since the comment reads its diff. The lessons of
        a task that keeps memory come next (LH-605), so nobody waits for a model. The note for the
        metrics comes last, so it counts a task that has finished all of this.

        Each step is best effort. An activity that fails for good is logged, and the next step
        goes on, so one failure leaves the outcome as it is and does not leave the rest undone. A
        cancel that arrives now is not acted on: the task is over. For that reason the steps do
        not go through `_call`, which stops at once on a cancel.
        """
        if not workflow.patched(OUTCOME_PATCH):
            await self._note_outcome(outcome)
            return outcome
        state = self.state
        await self._try(
            "cleanup",
            self._wind_call(
                acts.CLEANUP,
                self._cleanup_input(outcome, keep_repo=self._repo_outlives_the_sandbox(outcome)),
            ),
        )
        if lifecycle.is_github_task(state.spec):
            outcome = await self._open_draft(outcome)
            await self._report(outcome)
            await self._end_labels(outcome)
        await self._remove_repo(outcome)
        await self._lessons(outcome)
        await self._note_outcome(outcome)
        return outcome

    def _cancelled_by_a_person(self, outcome: Outcome) -> bool:
        return outcome.kind is OutcomeKind.CANCELLED and self.cancel_request is not None

    def _cleanup_input(self, outcome: Outcome, *, keep_repo: bool) -> acts.CleanupInput:
        """What to destroy when the task ends, and whether its repo stays.

        A task that ended on its own keeps its bare repo for a week, so that a person can read
        its checkpoints, and the janitor removes it. A task that a person cancelled has its repo
        removed, as a cancel always did, and its reason is the cancel reason.
        """
        request = self.cancel_request
        cancelled = self._cancelled_by_a_person(outcome)
        return acts.CleanupInput(
            task_id=self.state.spec.task_id,
            sandbox_id=self.state.sandbox_id,
            reason=request.reason
            if cancelled and request is not None
            else wind_up.cleanup_reason(outcome.kind),
            keep_repo=keep_repo,
        )

    def _repo_outlives_the_sandbox(self, outcome: Outcome) -> bool:
        """Whether the first cleanup leaves the repo, because the final comment reads its diff.

        A task of an issue posts a report that needs the repo, so the sandbox goes first and the
        repo of a cancelled one goes after the report (`_remove_repo`). A local task has no
        report, and one cleanup removes both.
        """
        return not self._cancelled_by_a_person(outcome) or lifecycle.is_github_task(self.state.spec)

    async def _remove_repo(self, outcome: Outcome) -> None:
        """Remove the bare repo of a task that a person cancelled, now that its report is out.

        They asked it to stop, so the repo does not wait a week for the janitor. The second
        cleanup finds no sandbox and removes the repo and the rest of what the task left.
        """
        if self._cancelled_by_a_person(outcome) and lifecycle.is_github_task(self.state.spec):
            await self._try(
                "repo cleanup",
                self._wind_call(acts.CLEANUP, self._cleanup_input(outcome, keep_repo=False)),
            )

    async def _open_draft(self, outcome: Outcome) -> Outcome:
        """Open a draft pull request with the partial work of a stuck task, if the repo asked.

        The work is the same change a finished task delivers, so it goes through the same two
        activities. A task whose work changes no file has nothing to open (the same error as in
        `_deliver`), and a draft that cannot be opened is logged: the report goes out without it.
        """
        state = self.state
        base_sha, issue = state.spec.base_sha, state.spec.issue_number
        asked = bool(workflow.memo_value(acts.OPEN_DRAFT_MEMO_KEY, False, type_hint=bool))
        if (
            outcome.kind is not OutcomeKind.STUCK
            or not asked
            or state.head_sha is None
            or base_sha is None
            or issue is None
        ):
            return outcome
        task_id, repo = state.spec.task_id, state.spec.repo
        summary = wind_up.draft_summary(outcome)
        try:
            built = await self._wind_call(
                acts.BUILD_PR_BRANCH,
                acts.BuildPrBranchInput(
                    task_id=task_id,
                    repo=repo,
                    summary=summary,
                    base_sha=base_sha,
                    head_sha=state.head_sha,
                    plan=state.plan,
                ),
            )
            url = await self._wind_call(
                acts.DELIVER,
                acts.DeliverInput(
                    task_id=task_id,
                    repo=repo,
                    issue_number=issue,
                    summary=summary,
                    branch=built.branch,
                    base_branch=built.base_branch,
                    base_sha=built.base_sha,
                    head_sha=built.head_sha,
                    title=built.title,
                    partial=True,
                ),
            )
        except ActivityError as err:
            if not _has_nothing_to_deliver(err):
                workflow.logger.warning(
                    "no draft pull request for stuck task %s: %s", task_id, err.cause or err
                )
            return outcome
        drafted = outcome.model_copy(update={"pr_url": url})
        self.outcome = drafted
        return drafted

    async def _report(self, outcome: Outcome) -> None:
        """Post the one final comment of a task of an issue."""
        state = self.state
        issue = state.spec.issue_number
        if issue is None:  # is_github_task has checked
            return
        await self._try(
            "final comment",
            self._wind_call(
                acts.POST_FINAL,
                acts.PostFinalInput(
                    task_id=state.spec.task_id,
                    repo=state.spec.repo,
                    number=issue,
                    outcome=outcome,
                    plan=state.plan,
                    base_sha=state.spec.base_sha,
                    head_sha=state.head_sha,
                    turns=state.turn_index,
                    cost_usd=state.cost_usd,
                    draft=not self._verified(outcome),
                ),
            ),
        )

    def _verified(self, outcome: Outcome) -> bool:
        """Whether the pull request of the outcome is ready for review and not a draft.

        `deliver` opens a ready pull request exactly when `verified_tests` is set, and only the
        delivery of a finished task passes it. The draft of a stuck task holds partial work and is
        always a draft.
        """
        return outcome.kind is OutcomeKind.PR_OPENED and self.verified_tests is not None

    async def _lessons(self, outcome: Outcome) -> None:
        """Draft the lessons of the task and record them (LH-605). Best effort, like the rest.

        Only a task that loaded its memory block asks: it ran on a worker with a store, and that
        worker registers the activity. The patch is checked after that test, so a task with no
        memory records no marker and its history stays as it was.
        """
        state = self.state
        if state.memory_ref is None or not workflow.patched(LESSONS_PATCH):
            return
        try:
            drafted = await self._wind_call(
                acts.EXTRACT_LESSONS,
                acts.ExtractLessonsInput(
                    task_id=state.spec.task_id,
                    turn_index=state.turn_index,
                    spec=state.spec,
                    outcome=outcome,
                    plan=state.plan,
                    notes=state.notes,
                    script_ref=self._script(),
                ),
            )
        except FailureError as err:
            workflow.logger.warning(
                "no lessons for task %s: %s", state.spec.task_id, err.cause or err
            )
            return
        state.cost_usd += drafted.cost_usd
        if drafted.status == "skipped":
            return
        await self._wind_note(
            acts.LESSONS_EVENT,
            {
                "status": drafted.status,
                "source_ref": drafted.source_ref,
                "count": drafted.stored,
                "items": [item.model_dump() for item in drafted.items],
            },
        )

    async def _end_labels(self, outcome: Outcome) -> None:
        opened = outcome.kind is OutcomeKind.STUCK and outcome.pr_url is not None
        for step in lifecycle.on_end(self.state.spec, outcome.kind, pr_opened=opened):
            await self._try(
                "label",
                self._wind_call(acts.ADD_LABEL if step.add else acts.REMOVE_LABEL, step.label),
            )

    async def _wind_note(self, event_type: str, payload: dict[str, Any]) -> None:
        """Record an event in the wind-up, which a cancel request does not interrupt."""
        note = acts.RecordEventInput(
            task_id=self.state.spec.task_id, type=event_type, payload=payload
        )
        await self._try(event_type, self._wind_call(acts.RECORD_EVENT, note))

    async def _wind_call[In, Out](self, act: acts.Act[In, Out], arg: In) -> Out:
        """Run an activity by name, outside the cancel handling of `_call`."""
        if act.local:
            local: Out = await workflow.execute_local_activity(
                act.name, arg, result_type=act.result_type, **acts.local_options(act)
            )
            return local
        remote: Out = await workflow.execute_activity(
            act.name, arg, result_type=act.result_type, **acts.options(act, self._tuning())
        )
        return remote

    async def _try(self, what: str, step: Awaitable[object]) -> None:
        """Await one step of the wind-up. A step that fails for good is logged and skipped."""
        try:
            await step
        except FailureError as err:
            workflow.logger.warning(
                "wind-up step %s of task %s failed: %s",
                what,
                self.state.spec.task_id,
                err.cause or err,
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
        """Plan until the coder can start. Returns None then, or the outcome that ends the task.

        The planner writes a version of the plan. On a repo with the plan gate a person decides
        on it, and a rejection sends the planner back for the next version, in its own
        transcript, until the version is approved or the gate ends the task (see "The plan gate"
        in the module docstring). Without the gate the first version is the one the coder gets.
        """
        while True:
            ended = await self._write_plan()
            if ended is not None:
                return ended
            if not self._gated():
                break
            version = self.state.plan_version
            decision = await self._review(version)
            if isinstance(decision, Outcome):
                return decision
            if decision.approved:
                break
            if version >= planner.MAX_PLAN_VERSIONS:
                return self._end(planner.not_approved_outcome(), Phase.FAILED)
            self.revision_hints.append(
                f"{decision.by}: {planner.revision_request(version, decision.feedback)}"
            )
        self._start_coding()
        return None

    async def _write_plan(self) -> Outcome | None:
        """Run the planner until it submits a plan. Returns None once the plan is stored.

        An outcome means the task ends here: the planner failed its repair turn, used up its
        turns, ran out of the task's turn budget, or was stopped by one of the ends that the coder
        shares (a refusal, a full window, a lost sandbox). Each call is a version of its own,
        with its own turns and its own repair turn.
        """
        state = self.state
        state.phase = Phase.PLANNING
        failures = 0  # plans that were not valid so far, for the one repair turn (F14)
        idle = 0  # replies in a row that ended without a tool call
        for _ in range(acts.PLANNER_MAX_TURNS):
            if self._out_of_turns():
                return self._end(rules.turn_limit_outcome(), Phase.FAILED)
            ended = await self._afford(Role.PLANNER)
            if ended is not None:
                return ended
            index = state.turn_index
            sampled = await self._sample(index, Role.PLANNER)
            if isinstance(sampled, Outcome):
                state.turn_index = index + 1
                return self._end(sampled, Phase.FAILED)
            calls = rules.runnable_calls(sampled, sampled.tool_call_summaries)
            if not calls:
                verdict = rules.idle_turn(idle)
                idle = verdict.count
                ended = await self._idle_read_only(
                    index, sampled, verdict, Role.PLANNER, planner.NUDGE
                )
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
        return await self._read_only_calls(
            index, reply, calls, lambda run: planner.run_calls(calls, run, failures)
        )

    async def _read_only_calls[R: _Answered](
        self,
        index: int,
        reply: acts.LlmTurnOutput,
        calls: list[acts.ToolCallSummary],
        judge: Callable[[Callable[[acts.ToolCallSummary], Awaitable[ToolResult]]], Awaitable[R]],
    ) -> R | Outcome:
        """Run the calls of a turn of a role that cannot change the tree, and answer them all.

        `judge` is the role's run_calls with its collaborators bound. It is given the function that
        makes one tool call, read-only, and decides which calls run and what the control tool
        came to. Every call is answered in the transcript, the control call included, so that the
        stored transcript is paired whether the phase goes on or ends here. A sandbox that is lost
        for good ends the run, and the outcome is returned.
        """
        state = self.state
        finished: list[ToolResult] = []  # kept for the transcript if a call loses the sandbox

        async def run(call: acts.ToolCallSummary) -> ToolResult:
            result = await self._run_tool(index, call, read_only=True)
            finished.append(result)
            return result

        try:
            ran = await judge(run)
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

    async def _idle_read_only(
        self, index: int, reply: acts.LlmTurnOutput, verdict: rules.Idle, role: Role, nudge: str
    ) -> Outcome | None:
        """A reply of the planner or the verifier with no tool calls.

        The first gets a nudge, and a second ends the run.
        """
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
                user_text=nudge,
                window_tokens=self._window(),
            ),
        )
        state.turn_index = index + 1
        await self._relieve(index, reply.context_decision, role)
        return None

    async def _accept(self, plan: Plan) -> None:
        """Keep the plan the planner submitted and get ready for the coder.

        The plan is written to the `plans` table and, for a task of an issue, posted on the issue,
        before it is in `state`: `status()` shows a plan version only once the plan is stored and
        out. The planner's transcript stays in the state, because a person may reject the plan
        and the planner then revises it from what it has read. `_start_coding` drops it.
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

    def _start_coding(self) -> None:
        """The plan is final. The coder starts a transcript of its own, with no planner notes."""
        self.state.transcript_ref = None
        self.state.notes = None
        # The planner's request size is not the coder's (LH-702).
        self.state.last_input_tokens = 0

    # ---------- the plan gate ----------
    def _gated(self) -> bool:
        """True when a person has to approve the plan before the coder starts (LH-502)."""
        if not self.state.spec.settings.plan_approval_required:
            return False
        # A history from before the gate has no marker for it and replays without it (F13). The
        # call is made only when the setting is on, so no other history gets a marker.
        return workflow.patched(PLAN_GATE_PATCH)

    async def _review(self, version: int) -> PlanDecision | Outcome:
        """Hold version `version` of the plan for a person. Returns the decision, or the outcome.

        The outcome is the end of a wait that ran out. A cancel unwinds to the cancel path. The
        decision is recorded by an activity before the label comes off, and the label comes off
        for a rejection too: the person has answered, and the planner is at work on the next
        version. The label stays when the wait runs out, because a person is still needed.

        A decision that arrived while the planner was at work is taken as it is: nobody is asked,
        so no request is left on the issue and no label goes on or off.
        """
        state = self.state
        state.phase = Phase.AWAITING_PLAN_APPROVAL
        asked = self.plan_decision is None
        if asked:
            await self._ask_for_review(version)
        decision = await self._decision()
        if decision is None:
            return self._end(planner.undecided_outcome(acts.HINT_WAIT_HOURS), Phase.FAILED)
        await self._call(
            acts.DECIDE_PLAN,
            acts.DecidePlanInput(
                task_id=state.spec.task_id,
                version=version,
                approved=decision.approved,
                by=decision.by,
            ),
        )
        state.plan_approved = decision.approved
        if asked and lifecycle.is_github_task(state.spec):
            await self._label(lifecycle.on_plan_decided(state.spec, version))
        return decision

    async def _ask_for_review(self, version: int) -> None:
        """Put the needs-human label on the issue and say how to answer. A local task has none."""
        spec = self.state.spec
        if not lifecycle.is_github_task(spec):
            return
        await self._label(lifecycle.on_plan_review(spec, version))
        await self._call(
            acts.POST_COMMENT,
            lifecycle.plan_review_comment(spec, version, hours=acts.HINT_WAIT_HOURS),
        )

    async def _decision(self) -> PlanDecision | None:
        """Take the decision a person sent, waiting for it if none has arrived yet.

        None means the wait ran out. A decision that is already there is taken without a timer, so
        an approval sent during planning costs the history nothing. The decision is cleared when
        it is taken: it applies to one version.
        """
        self._stop_if_cancelled()
        if self.plan_decision is None:
            began = workflow.now()
            try:
                await workflow.wait_condition(
                    lambda: self.plan_decision is not None or self.cancel_request is not None,
                    timeout=acts.hint_wait(),
                )
            except TimeoutError:
                self._stop_if_cancelled()
                return None
            self._stop_if_cancelled()
            # The wall clock leaves out the time a person took to decide, as in a pause (LH-702).
            self.state.paused_s += (workflow.now() - began).total_seconds()
        decision, self.plan_decision = self.plan_decision, None
        return decision

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
        if state.enforce_budgets:
            return False  # check_budget judges the turns, and pauses instead of ending the task
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
        ended = await self._afford(Role.CODER)
        if ended is not None:
            return ended
        index = state.turn_index
        sampled = await self._sample(index)
        if isinstance(sampled, Outcome):
            state.turn_index = index + 1
            return self._end(sampled, Phase.FAILED)
        calls = rules.runnable_calls(sampled, sampled.tool_call_summaries)
        if not calls:
            return await self._idle_turn(index, sampled)

        self.idle = 0
        # Kept for the transcript if a later call loses the sandbox, and for the stuck detector.
        finished: list[ToolResult] = []

        async def run(call: acts.ToolCallSummary) -> ToolResult:
            return await self._run_tool(index, call)

        try:
            ran = await self._run_calls(index, calls, run, finished)
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
            if not ran.review:
                return self._finish(rules.finished(ran.finish))
            ended = await self._verify(ran.finish)
            if ended is None:  # sent back: the coder goes on, as after any other turn
                await self._relieve(index, sampled.context_decision)
            return ended
        stuck_now: StuckSignal | None
        held = _held_for_approval(finished)
        if ran.question is not None:
            stuck_now = StuckSignal(reason=StuckReason.AGENT_ASKED, evidence=[ran.question])
        elif held is not None:
            # A gateway holds a call for a person (LH-909). The pause asks them as it does for
            # `ask_human`, with the gateway's reason as the question.
            stuck_now = StuckSignal(reason=StuckReason.AGENT_ASKED, evidence=[held])
        else:
            stuck_now = await self._watch(index, calls, ran.results, finished)
        if stuck_now is not None:
            ended = await self._pause(stuck_now)
            if ended is not None:
                return ended
        await self._relieve(index, sampled.context_decision)
        return None

    async def _run_calls(
        self,
        index: int,
        calls: list[acts.ToolCallSummary],
        run: Callable[[acts.ToolCallSummary], Awaitable[ToolResult]],
        finished: list[ToolResult],
    ) -> _Ran:
        """Run the calls of a coder turn by the rules of its kind: with a plan, or without.

        `finished` collects the answer of every call as it is made, so that a sandbox lost for good
        in the middle of the turn (`_end_lost`) still has the answers of the calls before it. For a
        task with a plan that includes a step update, which has already changed the plan.
        """
        if self.state.plan is None:

            async def kept(call: acts.ToolCallSummary) -> ToolResult:
                result = await run(call)
                finished.append(result)
                return result

            plain = await rules.run_calls(calls, kept)
            return _Ran(plain.results, plain.finish)

        async def update(change: coder.StepUpdate) -> None:
            await self._update_step(index, change)

        planned = await coder.run_calls(calls, run, update, self._plan_now, answered=finished)
        return _Ran(planned.results, planned.submitted, planned.question, planned.review)

    def _plan_now(self) -> Plan:
        plan = self.state.plan
        if plan is None:
            raise ApplicationError("the task has no plan to update", non_retryable=True)
        return plan

    async def _update_step(self, index: int, change: coder.StepUpdate) -> None:
        """Record one step update, and then put the new status in the plan.

        The record comes first, so that `status()` and the next prompt never show a step as done
        before the checkpoint of its work is saved. A retry of the activity writes the same rows.
        """
        state = self.state
        plan = self._plan_now()
        if state.head_sha is None:
            # prepare_workspace sets it before the first turn. Asking again changes nothing.
            raise ApplicationError(
                "a step cannot be recorded: the run has no checkpoint", non_retryable=True
            )
        await self._call(
            acts.RECORD_STEP,
            acts.RecordStepInput(
                task_id=state.spec.task_id,
                step_id=change.step_id,
                status=change.status,
                note=change.note,
                head_sha=state.head_sha,
                turn_index=index,
            ),
        )
        state.plan = coder.with_status(plan, change)

    async def _watch(
        self,
        index: int,
        calls: list[acts.ToolCallSummary],
        results: list[ToolResult],
        ran: Sequence[ToolResult],
    ) -> StuckSignal | None:
        """Show the turn to the stuck detector, and record a signal that it gives (LH-505).

        `results` are the results of every call of the turn, and `ran` the answers made as the
        calls ran. The ones tool_exec made carry the facts, and a step update's answer has none.
        The window is written back to the state before the event, so a hand-over right after it
        carries the window. The signal that is returned is what `_pause` acts on.
        """
        state = self.state
        done = sum(1 for step in state.plan.steps if step.status == "done") if state.plan else 0
        facts = [
            r.signal for r in ran if isinstance(r, acts.ToolExecResult) and r.signal is not None
        ]
        signal, notes = stuck.read_turn(index, done, facts, results, {c.id: c.name for c in calls})
        detector = stuck.StuckDetector(stuck.DEFAULT_LIMITS, state.stuck_window)
        found = detector.observe(signal, notes, phase=state.phase)
        state.stuck_window = detector.window
        if found is not None:
            await self._note(
                STUCK_EVENT,
                {
                    "reason": found.reason.value,
                    "evidence": found.evidence,
                    "turn_index": index,
                },
            )
        return found

    # ---------- the verifying phase ----------
    async def _verify(self, summary: str) -> Outcome | None:
        """Check the work the coder submitted. Returns None when the coder is sent back to fix it.

        Phase VERIFYING. The pre-check and the diff come first (`_gather`), then a verifier with a
        transcript of its own reads them and gives a verdict (`_run_verifier`). The work passes only
        when every command of the pre-check passed and the verdict passes (verifier.judge_review).
        A pass ends the coding like `finish` did, with the counts of the verdict kept for the
        pull request. A fail counts a round, and the coder is sent back with the issues, until the
        rounds run out and the task ends as stuck with the last review as its evidence.

        The coder's transcript and notes are set aside for the review and put back at the end,
        whatever the end is, because the state has one transcript and the verifier needs a fresh
        one.
        """
        state = self.state
        plan = self._plan_now()
        if self._out_of_turns():
            # The verifier needs a turn, so no command of the pre-check could be used. Each one can
            # take minutes, and the task ends the same way once the review has started.
            return self._end(rules.turn_limit_outcome(), Phase.FAILED)
        state.phase = Phase.VERIFYING
        number = state.verify_rounds + 1
        coder_view = (state.transcript_ref, state.notes, state.last_input_tokens)
        try:
            brief = await self._gather(number, plan)
            if isinstance(brief, Outcome):
                return brief
            got = await self._run_verifier(plan, brief)
        finally:
            state.transcript_ref, state.notes, state.last_input_tokens = coder_view
        if isinstance(got, Outcome):
            return got
        review = verifier.judge_review(brief.checks, got)
        await self._note(
            VERDICT_EVENT,
            {**verifier.verdict_event(number, review), "turn_index": state.turn_index},
        )
        if review.passed:
            # With no command in the pre-check the counts are the model's word only. The work may
            # pass, but the pull request stays a draft and does not say it was verified.
            self.verified_tests = review.tests_summary if brief.checks else None
            return self._finish(rules.finished(summary))
        state.verify_rounds += 1
        if state.verify_rounds >= acts.MAX_VERIFY_ROUNDS:
            return self._end(verifier.stuck_outcome(state.verify_rounds, review), Phase.FAILED)
        await self._send_back(review)
        return None

    async def _gather(self, number: int, plan: Plan) -> acts.ReviewBrief | Outcome:
        """Run the pre-check and take the diff, all read-only, before the verifier reads anything.

        The commands are the test command of the repo and the acceptance commands of the plan
        (verifier.precheck_commands), one after another, so a command that changes a file cannot
        change what the next one sees: the tree is put back after each. They belong to no model
        turn, so they carry the index of the verifier's first turn, which only says where their
        output blobs are kept. A sandbox that is lost for good ends the run.
        """
        state = self.state
        index = state.turn_index
        checks: list[acts.CheckRun] = []
        try:
            for command in verifier.precheck_commands(
                plan, state.spec.settings.test_command, state.spec.task_id, number
            ):
                call = acts.ToolCallSummary(
                    id=command.call_id, name=command.tool, input=command.arguments
                )
                result = await self._run_tool(index, call, read_only=True)
                # A command that did not finish has no exit code worth reading.
                exit_code = result.exit_code if result.ok else None
                checks.append(
                    acts.CheckRun(
                        command=command.label,
                        call_id=command.call_id,
                        passed=exit_code == 0,
                        exit_code=exit_code,
                        output_ref=result.output_ref,
                    )
                )
            diff_id = verifier.diff_call_id(state.spec.task_id, number)
            diff_call = acts.ToolCallSummary(id=diff_id, name=verifier.GIT_DIFF, input={})
            diff = await self._run_tool(index, diff_call, read_only=True)
        except _SandboxLost:
            return self._end(rules.error(SANDBOX_LOST), Phase.FAILED)
        shown = acts.CheckRun(
            command=verifier.GIT_DIFF, call_id=diff_id, passed=diff.ok, output_ref=diff.output_ref
        )
        return acts.ReviewBrief(checks=checks, diff=shown)

    async def _run_verifier(self, plan: Plan, brief: acts.ReviewBrief) -> VerifierVerdict | Outcome:
        """Run the verifier until it submits a verdict. An outcome means the task ends here.

        The verifier has a transcript of its own, so it starts from none and keeps no notes. An
        invalid verdict gets one repair turn (F14), and a second one ends the task with ERROR
        "verifier submitted an invalid verdict twice: ...". A verifier that has not submitted after
        verifier.MAX_TURNS turns ends it with ERROR "verifier submitted no verdict in 15 turns".
        The turn budget of the task applies to these turns as to any other, and so do the budgets
        of a task that enforces them (LH-702): each turn is checked first, and a limit pauses the
        task in the review.
        """
        state = self.state
        state.transcript_ref = None
        state.notes = None
        # The coder's request size is not the verifier's (LH-702). `_verify` puts it back.
        state.last_input_tokens = 0
        failures = 0  # verdicts that were not valid so far, for the one repair turn
        idle = 0  # replies in a row that ended without a tool call
        for _ in range(verifier.MAX_TURNS):
            if self._out_of_turns():
                return self._end(rules.turn_limit_outcome(), Phase.FAILED)
            ended = await self._afford(Role.VERIFIER)
            if ended is not None:
                return ended
            index = state.turn_index
            sampled = await self._sample(index, Role.VERIFIER, review=brief)
            if isinstance(sampled, Outcome):
                state.turn_index = index + 1
                return self._end(sampled, Phase.FAILED)
            calls = rules.runnable_calls(sampled, sampled.tool_call_summaries)
            if not calls:
                verdict = rules.idle_turn(idle)
                idle = verdict.count
                ended = await self._idle_read_only(
                    index, sampled, verdict, Role.VERIFIER, verifier.NUDGE
                )
                if ended is not None:
                    return ended
                continue

            idle = 0
            ran = await self._verifier_calls(index, sampled, calls, failures, plan)
            if isinstance(ran, Outcome):
                return ran  # the sandbox was lost for good
            failures = ran.failures
            if ran.outcome is not None:
                return self._end(ran.outcome, Phase.FAILED)
            if ran.verdict is not None:
                return ran.verdict
            await self._relieve(index, sampled.context_decision, Role.VERIFIER)
        return self._end(verifier.no_verdict_outcome(verifier.MAX_TURNS), Phase.FAILED)

    async def _verifier_calls(
        self,
        index: int,
        reply: acts.LlmTurnOutput,
        calls: list[acts.ToolCallSummary],
        failures: int,
        plan: Plan,
    ) -> verifier.VerdictCalls | Outcome:
        """Run the calls of one verifier turn, read-only, and answer them all in the transcript."""
        return await self._read_only_calls(
            index, reply, calls, lambda run: verifier.run_calls(calls, run, plan, failures)
        )

    async def _send_back(self, review: verifier.Review) -> None:
        """Tell the coder what the review found, as a user message at the end of its transcript.

        The message goes after the answers to the coder's last calls, where the API takes text
        from the harness (the nudge goes there too). It takes a turn index of its own: the log of
        the task has one part of results for each turn (context/log.py), and the coder's next
        turn would write over this one if they shared an index. The coder then goes on in the
        phase CODING, with the plan and the transcript it had.
        """
        state = self.state
        if state.transcript_ref is None:
            # The coder has made a turn to get here. Asking again changes nothing.
            raise ApplicationError(
                "the coder has no transcript to be sent back to", non_retryable=True
            )
        index = state.turn_index
        state.transcript_ref = await self._call(
            acts.APPEND_RESULTS,
            acts.AppendResultsInput(
                task_id=state.spec.task_id,
                turn_index=index,
                transcript_ref=state.transcript_ref,
                results=[],
                user_text=verifier.coder_message(review),
                window_tokens=self._window(),
            ),
        )
        state.turn_index = index + 1
        state.phase = Phase.CODING

    # ---------- the pause for a hint ----------
    async def _pause(self, signal: StuckSignal) -> Outcome | None:
        """Ask a person for help and wait. None when the task is unblocked and goes on.

        An outcome means the task ends here: it has had all its pauses, or nobody answered in
        time. A cancel during the wait unwinds to the cancel path.
        """
        state = self.state
        if not workflow.patched(PAUSE_PATCH):
            return await self._asked_before_the_pause(signal)
        if state.stuck_count >= stuck.MAX_PAUSES:
            return self._end(stuck.pauses_used_outcome(signal), Phase.FAILED)
        pause = state.stuck_count + 1
        wait = acts.hint_wait(self._tuning())
        # Counted before the first await. A hint that arrives while the question is written is
        # then an answer to it, and one that came before it answered nothing yet.
        seen = _Unanswered(len(state.hints), state.extra_budget_usd, state.extra_turns)
        state.phase = Phase.PAUSED
        self.paused_reason = f"waiting for a hint after {signal.reason.value.replace('_', ' ')}"
        began = workflow.now()
        await self._ask(signal, pause)
        try:
            await workflow.wait_condition(
                lambda: self._answered(seen) is not None or self.cancel_request is not None,
                timeout=wait,
            )
        except TimeoutError:
            self._stop_if_cancelled()
            return self._end(stuck.unanswered_outcome(signal, wait), Phase.FAILED)
        self._stop_if_cancelled()
        # The wall clock leaves out the time a person took to answer (LH-702).
        state.paused_s += (workflow.now() - began).total_seconds()
        await self._resume(pause, seen)
        return None

    # ---------- the budgets ----------
    async def _afford(self, role: Role, *, high_output_cap: bool = False) -> Outcome | None:
        """Check that the next model call fits the budgets, and wait while it does not (LH-702).

        `high_output_cap` is for the retry of a reply cut off at max_tokens, which may use more.

        None means the call may go ahead, now or after the waiting. An outcome means the task ends
        here: nobody lifted a limit in time, or the next call can never fit the repo's day. A
        cancel during the wait unwinds to the cancel path. A task that does not enforce budgets
        returns at once, and adds nothing to its history.
        """
        if not self.state.enforce_budgets:
            return None
        while True:
            stop = await self._budget_stop(role, high_output_cap)
            if stop is None:
                return None
            ended = await self._budget_pause(stop)
            if ended is not None:
                return ended

    async def _budget_stop(
        self, role: Role, high_output_cap: bool = False
    ) -> budget.BudgetVerdict | None:
        """The limit that the next call would cross, or None when it fits."""
        state = self.state
        limits = state.spec.settings.budgets
        now = workflow.now()
        worked = budget.elapsed(state.started_at, now, paused_s=state.paused_s)
        minutes = worked.total_seconds() / 60
        if budget.wall_clock_exceeded(worked, limits.task_wall_minutes):
            # The clock is judged here and not by the activity, so a replay sees the same time.
            numbers = budget.BudgetNumbers(
                spent_task_usd=state.cost_usd,
                task_usd_limit=limits.task_usd + state.extra_budget_usd,
                turns_done=state.turn_index,
                turns_limit=limits.task_turns + state.extra_turns,
                daily_repo_usd_limit=limits.daily_repo_usd,
                elapsed_minutes=minutes,
                wall_clock_minutes_limit=limits.task_wall_minutes,
            )
            return budget.BudgetVerdict(
                ok=False, reason=budget.BudgetReason.WALL_CLOCK, numbers=numbers
            )
        verdict = await self._call(
            acts.CHECK_BUDGET,
            acts.CheckBudgetInput(
                task_id=state.spec.task_id,
                repo=state.spec.repo,
                role=role,
                high_output_cap=high_output_cap,
                turn_index=state.turn_index,
                limits=limits,
                extra_usd=state.extra_budget_usd,
                extra_turns=state.extra_turns,
                spent_task_usd=state.cost_usd,
                last_input_tokens=state.last_input_tokens,
                now=now,
            ),
        )
        if verdict.ok:
            return None
        clock = {"elapsed_minutes": minutes, "wall_clock_minutes_limit": limits.task_wall_minutes}
        return verdict.model_copy(update={"numbers": verdict.numbers.model_copy(update=clock)})

    async def _budget_pause(self, stop: budget.BudgetVerdict) -> Outcome | None:
        """Tell people the budget stopped the task, wait, and resume. None when it goes on.

        An outcome ends the task: the wait for a top-up ran out, or the daily limit is smaller
        than the call, which tomorrow would not change.
        """
        state = self.state
        if stop.reason is None:  # _budget_stop gives only verdicts that are not ok
            raise ApplicationError("a budget pause without a reason", non_retryable=True)
        daily = stop.reason is budget.BudgetReason.DAILY_REPO_USD
        if daily and not stop.can_wait:
            return self._end(budget.unfit_outcome(stop), Phase.FAILED)
        state.budget_pauses += 1
        pause = state.budget_pauses
        before = state.phase
        wait = acts.hint_wait(self._tuning())
        # Counted before the first await, as in _pause: a top-up that arrives while the comment
        # is posted is an answer to it.
        seen = _Unanswered(len(state.hints), state.extra_budget_usd, state.extra_turns)
        began = workflow.now()
        midnight = budget.next_midnight(began)
        state.phase = Phase.PAUSED
        self.paused_reason = budget.pause_reason(stop)
        await self._announce_budget(stop, pause, wait, midnight)
        if daily:
            via = MIDNIGHT_VIA
            await self._wait_for_midnight(midnight)
        else:
            via = BUDGET_VIA
            try:
                await workflow.wait_condition(
                    lambda: self._topped_up(seen) or self.cancel_request is not None, timeout=wait
                )
            except TimeoutError:
                self._stop_if_cancelled()
                return self._end(budget.exhausted_outcome(stop, wait), Phase.FAILED)
        self._stop_if_cancelled()
        await self._budget_resume(stop, pause, before, began, seen, via)
        return None

    async def _announce_budget(
        self, stop: budget.BudgetVerdict, pause: int, wait: timedelta, midnight: datetime
    ) -> None:
        """Say why the task waits: on the issue for a task of one, and in the `paused` event."""
        state = self.state
        spec = state.spec
        reason = stop.reason
        if reason is None:
            return
        daily = reason is budget.BudgetReason.DAILY_REPO_USD
        payload: dict[str, Any] = {
            "reason": reason.value,
            "pause": pause,
            "turn_index": state.turn_index,
            "numbers": stop.numbers.model_dump(mode="json"),
        }
        if daily:
            payload["resume_at"] = midnight.isoformat()
        if lifecycle.is_github_task(spec) and spec.issue_number is not None:
            await self._call(
                acts.POST_BUDGET,
                acts.PostBudgetInput(
                    task_id=spec.task_id,
                    repo=spec.repo,
                    number=spec.issue_number,
                    pause=pause,
                    reason=reason,
                    numbers=stop.numbers,
                    wait_s=None if daily else wait.total_seconds(),
                    resume_at=midnight if daily else None,
                ),
            )
            if not daily:  # nobody needs to act on the daily limit, it ends by itself
                await self._label(lifecycle.on_budget_pause(spec, pause))
        # The event comes last, so that whoever sees it can rely on the comment and the label.
        await self._note(PAUSED_EVENT, payload)

    async def _wait_for_midnight(self, midnight: datetime) -> None:
        """Sleep on a durable timer until 00:00 UTC. A cancel wakes it, and nothing else does."""
        left = midnight - workflow.now()
        if left <= timedelta(0):
            return
        try:
            await workflow.wait_condition(lambda: self.cancel_request is not None, timeout=left)
        except TimeoutError:
            return  # the day has changed, which is what the task waited for

    def _topped_up(self, seen: _Unanswered) -> bool:
        state = self.state
        return state.extra_budget_usd > seen.usd or state.extra_turns > seen.turns

    async def _budget_resume(
        self,
        stop: budget.BudgetVerdict,
        pause: int,
        before: Phase,
        began: datetime,
        seen: _Unanswered,
        via: str,
    ) -> None:
        """Go back to the phase the task was in, with the time of the pause not counted."""
        state = self.state
        now = workflow.now()
        state.paused_s += (now - began).total_seconds()
        if stop.reason is budget.BudgetReason.WALL_CLOCK:
            # A new window: the task has worked no time at all as of now.
            whole = budget.elapsed(state.started_at, now, paused_s=0).total_seconds()
            state.paused_s = max(state.paused_s, whole)
        state.phase = before
        self.paused_reason = None
        if via == BUDGET_VIA and lifecycle.is_github_task(state.spec):
            await self._label(lifecycle.on_budget_resume(state.spec, pause))
        reason = stop.reason.value if stop.reason is not None else None
        await self._note(
            RESUMED_EVENT,
            {"pause": pause, "via": via, "reason": reason, "turn_index": state.turn_index},
        )
        if via == BUDGET_VIA:
            added: dict[str, Any] = {
                "pause": pause,
                "usd": state.extra_budget_usd - seen.usd,
                "turns": state.extra_turns - seen.turns,
            }
            if self.topped_up_by is not None:
                added["by"] = self.topped_up_by  # who the run report names for the top-up
            await self._note(BUDGET_ADDED_EVENT, added)

    async def _asked_before_the_pause(self, signal: StuckSignal) -> Outcome | None:
        """What a run did before the pause existed, for the histories that recorded it.

        A stuck signal had only been recorded, and `ask_human` recorded the question and ended
        the task as stuck in phase PAUSED.
        """
        if signal.reason is not StuckReason.AGENT_ASKED:
            return None
        question = signal.evidence[0]
        await self._note(
            PAUSED_EVENT,
            {"reason": ASKED_REASON, "question": question, "turn_index": self.state.turn_index},
        )
        return self._end(coder.asked_outcome(question), Phase.PAUSED)

    async def _ask(self, signal: StuckSignal, pause: int) -> None:
        """Write the question and show it: on the issue, or in the event of a local task."""
        state = self.state
        spec = state.spec
        asked = await self._call(
            acts.SUMMARIZE_FOR_HUMAN,
            acts.SummarizeInput(
                task_id=spec.task_id,
                turn_index=max(state.turn_index - 1, 0),
                spec=spec,
                transcript_ref=state.transcript_ref,
                notes=state.notes,
                reason=signal.reason,
                evidence=signal.evidence,
                pause=pause,
                script_ref=self._script(),
            ),
        )
        state.cost_usd += asked.cost_usd
        payload: dict[str, Any] = {
            "reason": signal.reason.value,
            "pause": pause,
            "question": asked.question,
            "turn_index": state.turn_index,
        }
        if lifecycle.is_github_task(spec):
            await self._call(acts.POST_COMMENT, lifecycle.stuck_comment(spec, pause, asked.comment))
            await self._needs_human(pause)
        else:
            payload["comment"] = asked.comment
        # The event comes last, so that whoever sees it can rely on the comment and the label.
        await self._note(PAUSED_EVENT, payload)

    def _answered(self, seen: _Unanswered) -> str | None:
        """How the pause was answered since `seen`: by a hint, by a budget top-up, or not yet."""
        state = self.state
        if len(state.hints) > seen.hints:
            return HINT_VIA
        if state.extra_budget_usd > seen.usd or state.extra_turns > seen.turns:
            return BUDGET_VIA
        return None

    async def _resume(self, pause: int, seen: _Unanswered) -> None:
        """Go back to coding from a clean slate.

        The hint is already in `state.hints`, and the next llm_turn shows it to the model. The
        detector starts again from an empty window, so a pattern that goes on builds up from
        nothing, and `stuck_count` counts the pause as spent.
        """
        state = self.state
        via = self._answered(seen)
        payload: dict[str, Any] = {"pause": pause, "via": via, "turn_index": state.turn_index}
        if via == HINT_VIA:
            payload["by"] = state.hints[seen.hints].split(": ", 1)[0]
        state.stuck_count += 1
        state.stuck_window = []
        state.phase = Phase.CODING
        self.paused_reason = None
        await self._human_done(pause)
        await self._note(RESUMED_EVENT, payload)

    async def _end_lost(
        self,
        index: int,
        transcript_ref: BlobRef,
        calls: list[acts.ToolCallSummary],
        finished: Sequence[ToolResult],
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

    def _hints_for(self, role: Role) -> list[str]:
        """The hints a model turn is given: the task's, and for the planner the rejections too."""
        hints = list(self.state.hints)
        if role is Role.PLANNER:
            hints.extend(self.revision_hints)
        return hints

    async def _sample(
        self, index: int, role: Role = Role.CODER, review: acts.ReviewBrief | None = None
    ) -> acts.LlmTurnOutput | Outcome:
        """Call the model until one reply can be used, or the run has to end (D8, D9).

        `review` is for the verifier: what its first message is built from.

        A prompt that is over the window is compacted down to the last two turns, and the turn
        is asked once more (F6). It is asked again only if the compaction changed the view. A
        second overflow, or one with nothing to fold, ends the run with "context limit".

        The caller has checked the budgets for the first call. Every call after it, a retry at the
        higher output cap, the continuation of a paused turn and the ask after a compaction, is
        checked here, because each is a model call that costs money (LH-702). A budget that ends
        the task comes back as the outcome.
        """
        state = self.state
        sampling = rules.Sampling()
        paused_ref: BlobRef | None = None
        compacted = False
        asked = False
        while True:
            if asked:
                ended = await self._afford(role, high_output_cap=sampling.retried)
                if ended is not None:
                    return ended
            asked = True
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
                        hints=self._hints_for(role),
                        high_output_cap=sampling.retried,
                        paused_ref=paused_ref,
                        script_ref=self._script(),
                        window_tokens=self._window(),
                        context_mode=self._context_mode(),
                        review=review,
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
            # The next request holds this one and the results of its calls, so the budget check
            # prices the larger of what the activity projected and what this one was billed at.
            state.last_input_tokens = max(
                reply.next_request_tokens, reply.request_tokens, reply.input_tokens_total
            )
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
                # The coder of a plan is not offered `finish`, so it is sent to its own tool.
                user_text=rules.NUDGE if state.plan is None else coder.NUDGE,
                window_tokens=self._window(),
            ),
        )
        state.turn_index = index + 1
        await self._relieve(index, reply.context_decision)
        return None

    async def _run_tool(
        self, index: int, call: acts.ToolCallSummary, *, read_only: bool = False
    ) -> acts.ToolExecResult:
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
        """Open the pull request and move the label. Ends the task with its URL.

        The pull request is ready for review when a verifier passed the work and a command of the
        pre-check backed it, and a draft otherwise (`verified_tests`, set by `_verify`).
        """
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
                    plan=state.plan,
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
                verified_tests=self.verified_tests,
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
        """Ask for a person on the issue, for a pause (LH-506, Phase 7): `pause` counts from 1."""
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
        if not workflow.patched(OUTCOME_PATCH):
            await self._clean_up_after_cancel(request)
        # The sandbox of a cancelled task is destroyed with that of every other end, in _wind_up.
        outcome = Outcome(
            kind=OutcomeKind.CANCELLED, summary=cancel_summary(request.by, request.reason)
        )
        return self._end(outcome, Phase.CANCELLED)

    async def _clean_up_after_cancel(self, request: CancelRequest) -> None:
        """What a cancel did before LH-507, for the histories that recorded it."""
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
        self.paused_reason = None
        self.outcome = outcome
        return outcome
