# ADR-0007: Roles as phases inside one workflow

- Status: Accepted
- Date: 2026-11-04

## Context

A task needs three jobs done by a model: plan the change, make it, and check it. The jobs need different instructions, different tools and different outputs. The planner reads and ends with a plan. The coder edits and ends with a summary. The verifier runs the tests and ends with a verdict (LH-504). A coder that may also plan can start editing before anyone has read its approach, and a planner that may edit can leave changes that nobody asked for.

The way the rest of the system is built decides how roles can be arranged:

- One task is one workflow with the task id as its workflow id (ADR-0004). Every signal (`cancel`, `hint`, `add_budget`, `approve_plan`) goes to that id.
- The unit of durability is one model turn and one tool call, each an activity with its own retry policy (ADR-0003). Every turn is a row in `turns`, and every payload larger than 16 KB is a blob (ADR-0005). Both are keyed by the task and the turn index.
- One task has one sandbox and one bare repo of checkpoints (ADR-0006, ADR-0009). A second sandbox would need its own volume and its own run of the setup command, which installs the dependencies of the repo.
- Budgets are per task: cost, turns and wall time. A pause or a stop applies to the task, not to a part of it.
- A workflow hands over to a new run every 40 turns (ADR-0005), and the new run starts from `CarryState`.

## Decision

Planner, coder and verifier are role prompts with tool subsets inside the same `TaskWorkflow`. They share one sandbox and one blob store. A role is three things: an entry in `build_system_prompt`, a tool list (`LlmActivities.role_tools`), and a phase of the workflow. `llm_turn` takes the role as an input and builds the request from it.

LH-501 adds the first of the other roles, the planner:

- **Phase PLANNING runs between PREPARING and CODING.** The planner has the read and run tools (`list_dir`, `read_file`, `search`, `run_tests`, `run_command`, `read_output`) and the control tool `submit_plan`, whose input schema is the `Plan` contract. It has at most 20 turns. The workflow cannot read the config file, so it keeps the number in `acts.PLANNER_MAX_TURNS`, and `loop.planner_max_turns` has to match it: a worker whose config holds another number stops before it takes a task.
- **Each role has a transcript of its own.** The coder starts a new transcript when the plan is accepted and sees none of the planner's reading. The turn index keeps counting through the phases, because rows and blobs are keyed by it and the task's turn budget counts every turn. The log of the task (ADR-0013) holds all of them in order.
- **The planner cannot change the tree.** It is not offered `edit_file` or `write_file`, and the workflow answers a call outside its tool set without running it. `run_command` and `run_tests` are shell commands, so they could write anything. Every planner call is marked read-only, and `tool_exec` puts the workspace back on the checkpoint with `restore(pre_sha, keep_ignored=True)` after any call that changed it. The files that git ignores stay, because they are what the setup built and the network is off by now. No checkpoint is made for such a call. The planner is told that what its commands write is undone.
- **The workflow judges `submit_plan` itself**, as it does `finish`. Nothing runs in the sandbox for it. A plan is checked against the contract and against the rules a schema cannot say: 2 to 8 steps, unique step ids, a `done_when` on every step and at least one acceptance check. An invalid plan gets one repair turn with the validation error as the answer to the call (F14). A second failure ends the task with an `error` outcome whose summary names the problems.
- **A valid plan is stored twice.** `store_plan` writes version 1 to the `plans` table, and the workflow keeps it in `CarryState.plan` with `plan_version`, which `status()` shows. For a task of an issue, `post_plan` posts the plan as a checklist under the idempotency key `<task>:plan_comment:<version>`.
- **Planning is a switch of the task.** A task started with `plan=True` has the key `plan` in its workflow memo, and runs the planner. The poller and `longhaul run --plan` set it. A task started without it goes straight to coding. The memo carries over Continue-As-New, and a run that has a plan does not plan again.

## Consequences

Easier:

- Cancel, hints, budgets, cost and the hand-over every 40 turns work for every role with no new plumbing, because there is one workflow and one history.
- The handoff from the planner to the coder is a value in `CarryState`. The sandbox needs no handoff, and the coder starts where the planner left the tree: the checkpoint.
- The plan gate (LH-502) is a wait inside the same workflow, and the verifier (LH-504) can send work back to the coder by changing the phase.
  - The gate holds the plan in the phase `AWAITING_PLAN_APPROVAL` for up to `stuck.hint_wait_hours`. A rejection goes back to the planner as a message in its own transcript, which the workflow kept for that reason, and the planner writes the next version, at most three. Because one workflow owns the planner, the wait and the coder, the signals of a person (`approve_plan`, `reject_plan`, `cancel`) reach all of them at the same id, and a decision that arrives during planning is still there when the gate asks.

Harder:

- `task.py` sequences every phase, so it grows with each role. The pure parts of a role (prompt, tool list, how its output is judged) live in `roles/` and are imported by the workflow, which keeps the workflow to sequencing and activity calls.
- Planner turns add to the history. Twenty turns with a few tool calls each stay well under the 40 turns at which a run hands over, and the hand-over only carries the coder's transcript and the plan.
- Each role has its own system prompt and tool list, so each transcript has its own prompt cache prefix. The coder does not reuse the planner's cache.
- The read-only guarantee rests on the reset after each call, and not on the planner being unable to run a command that writes. A command that writes outside `/workspace`, in `/tmp` for example, is not undone. That is the same reach a coder's command has.
- Adding a phase to a deployed workflow needs `workflow.patched` (F13). The planning switch in the memo keeps the runs that started without it as they were.

Watch from now on: the cost of the planner on short tasks. The planner's `effort` is the setting to lower. The turn limit is fixed at 20 until the workflow can take it from the config.

## Alternatives

- **A child workflow for each role.** A child needs its own transcript and its own history. It needs a sandbox of its own or a handoff of the sandbox from one workflow to another, and it needs the signals of the parent forwarded to it, with cancel and budgets spread across both. The task's cost, turns and report would have to be joined from several workflows. None of that buys anything at this scale, where the roles run one after another and never at the same time.
- **One role with every tool, told to plan first.** The planner could edit, and nothing would stop it. There would be no point at which a person can read the plan before the coder starts, and the tool list would stay the coder's for the whole task, so the planner's guarantee would be a request in a prompt and not a rule.
- **A planning job before the workflow starts.** The plan would be made outside Temporal, without retries, without a checkpointed sandbox and without the budget of the task. The planner could not run the tests to reproduce the failure, because the sandbox does not exist yet.
