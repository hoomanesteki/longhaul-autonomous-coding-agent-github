# ADR-0004: Temporal for durable execution

- Status: Accepted
- Date: 2026-10-30

## Context

A Longhaul task runs for minutes or hours. It makes dozens of model calls and tool calls. It waits for people for up to 24 hours (plan approval, hints, budget top-ups). If a worker dies, the task must carry on without redoing finished work and without repeating a side effect.

The runtime needs these things:

- workflow state that survives a process crash
- a timeout, a retry policy and a heartbeat for each model turn and each tool call
- timers that last hours
- signals and queries for approve, reject, hint, add budget, cancel and status
- a scheduler for the GitHub poller
- a way to reset a long history (Continue-As-New)
- one place to look at what happened (the Temporal UI)

Spike LH-201 checked the SDK patterns that Phase 2 depends on. The results are in the appendix at the end of this file.

## Decision

Use Temporal, with the Python SDK `temporalio`, locked at 1.34.0 in `uv.lock`.

- One task is one workflow, `TaskWorkflow`. The workflow id is the task id.
- Workflow code is deterministic. It schedules activities and does no I/O (invariant I1). All I/O lives in activities.
- Each model turn and each tool call is its own activity with its own timeout, heartbeat and retry policy (plan section 3.7).
- Payloads are Pydantic models, encoded by `temporalio.contrib.pydantic.pydantic_data_converter`. Anything over 16 KB crosses an activity boundary as a `BlobRef` (ADR-0005).
- Long runs hand off with Continue-As-New and carry `CarryState`.
- The GitHub poller is a Temporal Schedule with overlap policy SKIP (ADR-0008).
- Tests use `temporalio.testing.WorkflowEnvironment`. `start_time_skipping()` covers long timers. `start_local()` covers anything that needs schedules.
- The local stack runs the Temporal dev server from `docker-compose.yml` (`temporalio/temporal:1.5.1`, which ships Server 1.29.1).

## Consequences

Easier:

- A crash costs one retried activity, not the whole run. The measured delay is in appendix section 3.
- Timers, signals, queries, schedules and a history UI come with the server. We do not build them.
- Replay catches workflow code that stopped being deterministic.

Harder:

- Workflow code has rules: no I/O, no clock, no random, no unsafe imports. Invariant I1 enforces them. The appendix shows that the SDK sandbox helps but is not enough on its own.
- Changing a deployed workflow needs `workflow.patched` and a replay test (failure mode F13).
- One more service to run and one more thing to test against. The SDK test servers do not match the compose server (Server 1.32.0 for `start_local()`, Server 1.29.1 in compose, and a separate Java test server for time skipping, which has no schedules).
- After a hard crash, an activity restarts only after its heartbeat timeout. That is up to 15 to 60 s per crash with the values in section 3.7 of the plan.

Watch from now on:

- The determinism rules, through the I1 test and the replay test.
- The server versions used by tests and by compose. Upgrade them together in a `chore/` PR.
- The size of every payload and of every history (invariant I2 and Continue-As-New).

## Alternatives

- **Celery or RQ.** They run jobs, but they keep no workflow state. We would have to store state, timers, signals and retries ourselves, and a crash in the middle of a job would lose its place.
- **A state machine in Postgres.** It is possible, and it would be easy to inspect. We would rebuild retries with backoff, durable timers, signals, queries, schedules and a history view. That is more code than the agent itself.
- **LangGraph with a Postgres checkpointer.** It can checkpoint each graph node (see ADR-0003). We would still need a job runner, 24 hour timers, schedules, signals and a history UI. Temporal gives us all of those.

## Appendix: SDK facts

LH-201 checked the SDK patterns that Phase 2 depends on. The throwaway script is `spikes/temporal_patterns.py`. Run `uv run python spikes/temporal_patterns.py all` to repeat it. Each part runs one pattern for real and prints what it saw. The numbers below come from one run on an Apple silicon laptop on 2026-10-30.

| Item | Value |
|---|---|
| `temporalio` | 1.34.0 (`uv.lock`) |
| Python and pydantic | 3.12.13 and 2.14.1 |
| `WorkflowEnvironment.start_local()` | Temporal CLI 1.9.1 with Server 1.32.0. The SDK downloads it on first use (150 MB). |
| `WorkflowEnvironment.start_time_skipping()` | A Java test server. The SDK downloads it on first use (65 MB). |
| `docker-compose.yml` | `temporalio/temporal:1.5.1`, which ships Server 1.29.1 |

Both downloads land in the system temp directory and need network access the first time. CI must allow that or cache the files. `dev_server_existing_path` and `test_server_existing_path` point the SDK at a cached binary.

The parts that need a real server (2, 3, 4, 6 and 7) were repeated against the compose server, in a throwaway compose project named `lh201` that was removed afterwards. Only the lowered threshold test in part 4 was skipped, because it needs dev server flags. The answers were the same. The text says where a number differs.

### Answers in one line each

| # | Question | Answer |
|---|---|---|
| 1 | Sandbox imports | Pass `longhaul.contracts` through with `workflow.unsafe.imports_passed_through()`. It saves about 30 ms per workflow run and keeps one class object. The sandbox does not enforce I1 by itself. |
| 2 | Pydantic converter | `temporalio.contrib.pydantic.pydantic_data_converter` exists in 1.34.0. `CarryState` with a datetime round-trips through every boundary. No custom converter is needed. |
| 3 | Heartbeats | After `SIGKILL` the activity restarts about `heartbeat_timeout - age of the last heartbeat + 1 s` later: 3.4 s with a 3 s timeout, 13.0 s with 15 s. With no heartbeat timeout it waits for start-to-close (16.9 s with a 20 s limit). |
| 4 | Continue-As-New | `is_continue_as_new_suggested()` and `get_current_history_length()` both exist. The server suggests at 4096 events. Load carried state in `@workflow.init`. |
| 5 | Time skipping | `start_time_skipping()` skips time only while the test awaits a workflow result. A 24 hour timer took 0.08 s. It has no schedules. |
| 6 | Duplicate id | `temporalio.exceptions.WorkflowAlreadyStartedError`. Start with `REJECT_DUPLICATE` and `FAIL`. |
| 7 | Schedules | `client.create_schedule` with overlap `SKIP`. Install means create, and on `ScheduleAlreadyRunningError` update only when the config differs. |

### 1. Workflow sandbox import rules and `imports_passed_through()`

**Answer.** Pass `longhaul.contracts` through the sandbox. Do not rely on the sandbox to enforce invariant I1. Keep the AST test, and also block the I/O libraries on the worker.

How the sandbox works in 1.34.0:

- Every workflow run gets a fresh sandbox. The sandbox executes the workflow module again, and every module it imports, unless the module is on the pass-through list.
- The default list holds the standard library (except `sys`), `temporalio`, `pydantic`, `pydantic_core`, `typing_extensions`, `grpc` and `google.protobuf`. A passed-through module is the real module, not a copy.
- `longhaul.contracts` is not on the list. Without pass-through the sandbox builds its own copy of every contract model on every workflow run.

```python
from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from longhaul.contracts import CarryState, Outcome, TaskStatus
```

Measured for `longhaul.contracts`, one workflow run each:

| Setup | Import cost inside the sandbox | Same `CarryState` class as outside |
|---|---|---|
| default sandbox, no pass-through | 26 to 39 ms (three runs) | no |
| `imports_passed_through()` around the import | 0.2 ms | yes |
| `with_passthrough_modules("longhaul.contracts")` on the worker | under 0.1 ms | yes |

Both setups gave correct results with the pydantic converter (section 2). Pass-through is for speed and for class identity. With a separate copy, an `isinstance` check against the outside class fails for models built inside the sandbox.

What the sandbox blocks. Each of these raised `RestrictedWorkflowAccessError`:

- `datetime.now()`, `date.today()`, `random.random()`, `time.time()`, `time.monotonic()`
- `uuid.uuid4()`, `os.getenv()`, `os.listdir()`, `Path.exists()`
- `subprocess.run()`, `socket.socket()`, `threading.Thread()`

These were allowed: `random.Random(1).random()` (a seeded instance), `workflow.now()` (returns UTC), `workflow.uuid4()` and `workflow.random()`.

Two more behaviours to know:

- An uncaught `RestrictedWorkflowAccessError` does not fail the run. The workflow task fails with cause `WORKFLOW_TASK_FAILED_CAUSE_WORKFLOW_WORKER_UNHANDLED_FAILURE`, the run stays `RUNNING`, and the server retries the task with backoff. Fixing the code and restarting the worker lets the run continue.
- Every blocked access logs a WARNING (for example `__call__ on datetime.datetime.now restricted`), and every module imported after the first load emits a `UserWarning` (`Module X was imported after initial workflow load`). Both show up in worker logs, even when workflow code catches the error.

What the sandbox does not guarantee for I1:

- `docker`, `sqlalchemy`, `boto3`, `openai` and `httpx2` all failed to import inside the default sandbox. That is an accident. While they import, they touch restricted standard library members such as `http.client` and `platform`. A different library could import fine.
- The failure is slow on a cold start (from a few ms to 1.1 s for `sqlalchemy`) and the message points at the wrong thing (`Cannot access http.client.IncompleteRead.__mro_entries__`).
- With `invalid_modules` the same imports fail at once, in under 1 ms, with `Cannot access docker from inside a workflow`.
- A workflow module with `import docker` at the top makes `Worker(...)` fail at construction with `RuntimeError: Failed validating workflow BadWorkflow`. This held with and without `invalid_modules`, so a bad import never reaches a running worker.
- The I1 list in the plan names `httpx`. The lock file has `httpx2` instead, pulled in by `openai` 3.41.0. `httpx` is not installed. The AST test and the block list must name `httpx2`.

```python
import dataclasses

from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxMatcher,
    SandboxRestrictions,
)

IO_LIBRARIES = {
    "httpx", "httpx2", "docker", "sqlalchemy", "boto3", "botocore", "openai", "anthropic",
}
restrictions = dataclasses.replace(
    SandboxRestrictions.default.with_passthrough_modules("longhaul.contracts"),
    invalid_modules=SandboxMatcher(access=IO_LIBRARIES),
)
worker = Worker(
    client,
    task_queue="longhaul",
    workflows=[TaskWorkflow],
    activities=[...],
    workflow_runner=SandboxedWorkflowRunner(restrictions=restrictions),
)
```

### 2. The pydantic data converter

**Answer.** `temporalio.contrib.pydantic` exists in 1.34.0. It is one module that exports `pydantic_data_converter`, `PydanticPayloadConverter`, `PydanticJSONPlainPayloadConverter` and `ToJsonOptions`. We need no custom converter, so the `model_dump(mode="json")` fallback is not used.

```python
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment

client = await Client.connect(address, data_converter=pydantic_data_converter)
env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
```

The worker takes its converter from the client. Calling an activity by name, as the planned `acts.py` stubs do, works with pydantic models when the call names the result type:

```python
out = await workflow.execute_activity(
    "llm_turn", inp, result_type=LlmTurnOutput, start_to_close_timeout=timedelta(minutes=10)
)
```

Observed:

- The payload is `json/plain`. A populated `CarryState` was 1387 bytes. `started_at` is written as `"2026-10-30T07:15:30.123456Z"` and comes back as an aware datetime that compares equal. A naive datetime stays naive.
- A full trip returned the expected `CarryState` with every field intact: client start argument, workflow, activity argument and result, `continue_as_new` argument, workflow result. A query returning `TaskStatus` (with a nested `Outcome`) and the two-argument signals `hint(by, text)` and `add_budget(by, usd, turns)` also worked.
- The same trip worked with the contracts passed through and with the contracts reloaded by the sandbox.
- Without a result type the decode gives a plain `dict`. The by-name call above returned `CarryState` with `result_type` and `dict` without it. Always pass `result_type`.
- The default converter also handled `CarryState` in 1.34.0, but only through the pydantic v1 methods `dict()` and `parse_obj`. It warns on every encode (`If you're using Pydantic v2, use temporalio.contrib.pydantic.pydantic_data_converter`). Do not rely on it.

### 3. Activity heartbeats and `heartbeat_timeout` when the worker is killed

**Answer.** The server notices a dead worker only when a timeout fires. With a heartbeat timeout, the retry starts about `heartbeat_timeout - age of the last heartbeat the server saw + the retry interval` after the kill. Without one, it waits for start-to-close.

```python
# workflow side (the retry interval is the policy's initial interval)
await workflow.execute_activity(
    "tool_exec",
    inp,
    result_type=ToolResult,
    start_to_close_timeout=timedelta(seconds=tool_timeout_s + 60),
    heartbeat_timeout=timedelta(seconds=15),
    retry_policy=RetryPolicy(maximum_attempts=3),
)


# activity side: heartbeat from a background task and never block the event loop
@activity.defn
async def tool_exec(inp: ToolExecInput) -> ToolResult:
    async def beat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(5)

    beater = asyncio.create_task(beat())
    try:
        return await run_tool(inp)
    finally:
        beater.cancel()
```

The check: a worker in a subprocess runs an activity that heartbeats every second. The test kills it, starts a new worker at once, and records when attempt 2 starts. The retry policy has a 1 s interval.

| Case | Age of the last heartbeat the server saw | Kill to start of attempt 2 |
|---|---|---|
| `SIGKILL`, `heartbeat_timeout` 3 s | 0.6 s | 3.4 s |
| `SIGKILL`, `heartbeat_timeout` 15 s (the `tool_exec` value) | 3.0 s | 13.0 s |
| `SIGKILL`, no heartbeat timeout, start-to-close 20 s | 3.1 s | 16.9 s |
| `SIGTERM`, `graceful_shutdown_timeout` 2 s, `heartbeat_timeout` 15 s | 3.0 s | 3.0 s |
| `SIGKILL` while the workflow only waits on a 4 s timer (no activity) | not applicable | the workflow ended 10.1 s late |

The compose server gave 3.4 s, 13.0 s, 16.9 s and 3.1 s, and 11.0 s late for the timer case.

Observed:

- **Detection time.** 3.4 s is 3 - 0.6 + 1 (the retry interval). 13.0 s is 15 - 3.0 + 1. The server marks the attempt failed with `activity Heartbeat timeout`, waits the retry interval, then hands the next attempt to a worker that polls. The replacement worker was up by then, so it added no delay. Without a heartbeat timeout the failure is `activity StartToClose timeout`, 20.1 s after the attempt started, which is 20.1 - 4.2 + 1 = 16.9 s after the kill.
- **What a kill costs.** For the values in plan section 3.7 a hard crash costs up to 15 s (`tool_exec`), 30 s (`llm_turn`) or 60 s (`prepare_workspace`), plus the retry interval. The chaos tests (LH-209) should set shorter heartbeat timeouts in their config.
- **Heartbeats are throttled.** The SDK sends the first heartbeat at once, then at most one per `0.8 * heartbeat_timeout`, or one per 30 s when there is no timeout (the Worker docs say this). With a 15 s timeout the activity called `heartbeat(step)` four times, but the server held step 1, and attempt 2 read `activity.info().heartbeat_details == [1]`. Heartbeat details are a hint, not a progress record.
- **Graceful shutdown is fast.** `SIGTERM` made the worker exit 2.1 s later, the length of its `graceful_shutdown_timeout`. The attempt failed at once with `Worker is shutting down and this activity did not complete in time`, and attempt 2 started 3.0 s after the signal. The latest heartbeat details were flushed on the way out (attempt 2 read step 6). The Worker signature sets `graceful_shutdown_timeout` to zero by default. Per the Worker docs that cancels running activities as soon as shutdown starts. The spike did not run the default.
- **A blocked event loop looks like a dead worker.** An activity that heartbeats from a background task but then calls `time.sleep(6)` (a stand-in for a synchronous SDK call) was timed out after 3.2 s with `activity Heartbeat timeout`, although the worker was alive. Attempt 2 ran in the same process. Keep activity bodies async, and send blocking calls to `asyncio.to_thread`.
- **A kill between activities is slower.** The workflow only waited on a timer. After the kill, the timer's workflow task sat on the dead worker's sticky queue until the 10 s `sticky_queue_schedule_to_start_timeout` passed. The workflow finished 10.1 s late (11.0 s on compose). That is the recovery time for failure mode F2.

### 4. `is_continue_as_new_suggested()` and `get_current_history_length()`

**Answer.** Both exist in 1.34.0, together with `get_current_history_size()`. The fallback (turn count only) is not needed, but the turn count stays the main trigger, because the server suggests only at about 4096 events (see the notes at the end).

```python
# Sketch. The helper methods stand in for the real turn loop.
@workflow.defn
class TaskWorkflow:
    # @workflow.init runs before any signal handler. Load the carried state here, not in run().
    @workflow.init
    def __init__(self, state: CarryState) -> None:
        self.state = state

    @workflow.signal
    def hint(self, by: str, text: str) -> None:
        self.state.hints.append(f"{by}: {text}")

    @workflow.run
    async def run(self, state: CarryState) -> Outcome:
        while not self.finished():
            await self.do_turn()
            due = self.state.turn_index % self.every_n_turns == 0
            if due or workflow.info().is_continue_as_new_suggested():
                await workflow.wait_condition(workflow.all_handlers_finished)
                workflow.continue_as_new(self.state)
        return self.outcome()
```

Observed:

- **History growth.** One activity round trip adds 6 events. The history length after 1, 2, 10 and 100 loops was 9, 15, 63 and 603.
- **Server suggestion.** The flag first turned True at 4101 events (518,504 bytes), after 683 loops, on the SDK dev server (Server 1.32.0). The compose server (Server 1.29.1) and the Java test server did the same, at 4101 events. The size threshold did not trigger first.
- **Lower threshold for tests.** `dev_server_extra_args=["--dynamic-config-value", "limit.historyCount.suggestContinueAsNew=40"]` made the flag turn True at 45 events, and the workflow chained 3 runs under one id. A test can use this to cover the suggestion without 700 loops.
- **`ContinueAsNewError` derives from `BaseException`.** `except Exception` does not catch it. A bare `except:`, or an `await` in a `finally` around `continue_as_new`, would break the hand-off.
- **Signals during the hand-off.** A test sent a signal every 5 ms while a workflow continued as new 19 times. With the state loaded in `@workflow.init`, all 675 accepted signals survived (637 of 637 on compose). With the state loaded inside `run()`, only 235 of 1139 survived (399 of 477 on compose). The count lost depends on timing. The SDK applies signals that arrive with the first workflow task before `run()` starts, so `self.hints = list(state.hints)` in `run()` throws them away.
- **Racing signals are retried.** In each run the server rejected 11 to 15 hand-offs with `WORKFLOW_TASK_FAILED_CAUSE_UNHANDLED_COMMAND`, because a signal arrived while the task was finishing. The SDK replayed the task with the new signal in history. With the state loaded in `@workflow.init`, no hint was lost or duplicated.
- **Same workflow id.** A client handle without a run id (`client.get_workflow_handle(task_id)`) always reaches the current run. `handle.result()` follows the chain by default.

### 5. `WorkflowEnvironment.start_time_skipping()` with long timers

**Answer.** Time skips only while the test awaits a workflow result (`handle.result()` or `execute_workflow`). While the test does anything else, the clock runs in real time. Send signals before you await the result.

```python
async with await WorkflowEnvironment.start_time_skipping(
    data_converter=pydantic_data_converter
) as env:
    async with Worker(env.client, task_queue="t", workflows=[TaskWorkflow], activities=[...]):
        handle = await env.client.start_workflow(
            TaskWorkflow.run, state, id=state.spec.task_id, task_queue="t"
        )
        await handle.signal(TaskWorkflow.hint, args=["alice", "try pytest"])  # first
        outcome = await handle.result()  # time skips only while this await is pending
        await env.sleep(timedelta(hours=2))  # manual skip
        with env.auto_time_skipping_disabled():  # a plain context manager, not async
            ...
```

Observed:

| Check | Result |
|---|---|
| 24 hour `wait_condition` timeout, awaiting the result | the workflow saw 24 hours pass; 0.08 s of real time |
| test sleeps 1.5 s without awaiting the result | status `RUNNING`; the test clock moved 1.5 s |
| `env.sleep(timedelta(hours=2))` | the test clock moved 2 hours |
| activity retry backoff of 10 minutes | the workflow saw 10 minutes pass; 0.04 s of real time |
| real 3 s activity with `heartbeat_timeout` 10 s and no heartbeats | no timeout; attempt 1; no time was skipped while the activity ran |
| `auto_time_skipping_disabled()` and a 2 s timer | took 2.1 s |

More facts:

- **Read elapsed time inside the workflow.** After a few runs the test clock stood 3651 days ahead of the real clock, and 7301 days ahead after more runs. The cause was not investigated. Assert on `workflow.now()` differences returned by the workflow, not on `env.get_current_time()`.
- **No schedules.** `create_schedule` fails with `RPCError: Method temporal.api.workflowservice.v1.WorkflowService/CreateSchedule is unimplemented`. Poller tests (LH-307) need `start_local()` or the compose server.
- **The rest matches.** The Java test server suggested Continue-As-New at the same 4101 events, and the duplicate id policies in section 6 behaved the same.
- **Cost of the first start.** `start_time_skipping()` took 4.0 s on the first call, and `start_local()` took 10.9 s, both with the one-time download.

### 6. Starting a workflow with an id that already exists

**Answer.** `client.start_workflow` raises `temporalio.exceptions.WorkflowAlreadyStartedError`. It is a `FailureError`. It carries `workflow_id`, `workflow_type`, `run_id` (the existing run) and `first_run_id`. The message is `Workflow execution already started`. Start tasks with `id_reuse_policy=REJECT_DUPLICATE` and `id_conflict_policy=FAIL`.

```python
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError


async def start_task(client: Client, state: CarryState) -> WorkflowHandle[Any, Outcome]:
    return await client.start_workflow(
        TaskWorkflow.run,
        state,
        id=state.spec.task_id,
        task_queue="longhaul",
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
    )


# the poller treats a duplicate as "already handled"
try:
    await start_task(client, state)
except WorkflowAlreadyStartedError:
    pass
```

What each policy did on the CLI dev server. The compose server gave the same results for every row. The time skipping test server gave the same results for the three cases it was checked on (SDK defaults, `REJECT_DUPLICATE` with `FAIL`, and `USE_EXISTING`, all against a running workflow).

| Existing run | Start options | Result |
|---|---|---|
| running | SDK defaults | `WorkflowAlreadyStartedError` |
| running | `conflict=FAIL` | `WorkflowAlreadyStartedError` |
| running | `reuse=REJECT_DUPLICATE` | `WorkflowAlreadyStartedError` |
| running | `reuse=REJECT_DUPLICATE`, `conflict=FAIL` | `WorkflowAlreadyStartedError` (a valid pair) |
| running | `conflict=USE_EXISTING` | no error; the handle points at the existing run |
| running | `reuse=REJECT_DUPLICATE`, `conflict=USE_EXISTING` | no error; the handle points at the existing run |
| running | `conflict=TERMINATE_EXISTING` | the old run is terminated; a new run starts |
| running | `reuse=TERMINATE_IF_RUNNING` (old style) | the old run is terminated; a new run starts |
| completed | SDK defaults (`ALLOW_DUPLICATE`) | a new run starts |
| completed | `reuse=ALLOW_DUPLICATE_FAILED_ONLY` | `WorkflowAlreadyStartedError` |
| completed | `reuse=REJECT_DUPLICATE` | `WorkflowAlreadyStartedError` |
| failed | SDK defaults | a new run starts |
| failed | `reuse=ALLOW_DUPLICATE_FAILED_ONLY` | a new run starts |
| failed | `reuse=REJECT_DUPLICATE` | `WorkflowAlreadyStartedError` |
| terminated | SDK defaults | a new run starts |
| terminated | `reuse=ALLOW_DUPLICATE_FAILED_ONLY` | a new run starts |
| terminated | `reuse=REJECT_DUPLICATE` | `WorkflowAlreadyStartedError` |

Why `REJECT_DUPLICATE` with `FAIL`:

- The SDK defaults already reject a duplicate while a run is open. They allow a new run once the old one has closed. That would restart a finished task if the label is still on the issue.
- `REJECT_DUPLICATE` rejects in both cases with the same exception, so the poller needs one `except` clause. A retry uses a new id (`lh-...-a2`, LH-308), so it is not blocked.
- `REJECT_DUPLICATE` only lasts as long as the namespace keeps closed runs. The dev server keeps them for 1 day. The `tasks` table stays the long term record, and the poller must check it first.
- `USE_EXISTING` would hide a duplicate. `start_task` could not tell a new task from an old one.

### 7. The Schedules API for the poller

**Answer.** Use `client.create_schedule` with `SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP)`. `create_schedule` on an existing id raises `temporalio.client.ScheduleAlreadyRunningError` (message `Schedule already running`). To make `longhaul poller install` idempotent, catch that error and then call `handle.update` with a callback that returns `None` when nothing differs. Returning `None` sends no update.

```python
from temporalio.client import (
    Schedule, ScheduleActionStartWorkflow, ScheduleAlreadyRunningError, ScheduleIntervalSpec,
    ScheduleOverlapPolicy, SchedulePolicy, ScheduleSpec, ScheduleUpdate, ScheduleUpdateInput,
)


def poller_schedule(interval_s: int) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            PollerWorkflow.run, id="longhaul-poller-run", task_queue="longhaul"
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=interval_s))]),
        policy=SchedulePolicy(
            overlap=ScheduleOverlapPolicy.SKIP, catchup_window=timedelta(minutes=2)
        ),
    )


def fingerprint(schedule: Schedule) -> tuple[Any, ...]:
    """The parts that install owns. The described action holds names as strings."""
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    return (
        action.workflow,
        action.task_queue,
        action.id,
        tuple(i.every for i in schedule.spec.intervals),
        schedule.policy.overlap,
        schedule.policy.catchup_window,
    )


async def install(client: Client, desired: Schedule) -> str:
    try:
        await client.create_schedule("longhaul-poller", desired)
        return "created"
    except ScheduleAlreadyRunningError:
        pass
    changed = False

    def updater(inp: ScheduleUpdateInput) -> ScheduleUpdate | None:
        nonlocal changed
        current = inp.description.schedule
        if fingerprint(current) == fingerprint(desired):
            return None
        changed = True
        # keep the paused flag and note: an operator may have paused the schedule
        return ScheduleUpdate(schedule=dataclasses.replace(desired, state=current.state))

    await client.get_schedule_handle("longhaul-poller").update(updater)
    return "updated" if changed else "unchanged"
```

Observed:

- **Install.** Install #1 created the schedule. A second install with the same config returned "unchanged" and `last_updated_at` did not move. Changing the interval from 1 s to 2 s while the schedule was paused updated it, and the schedule stayed paused with its note. Installing the new config again changed nothing. The described action carries the workflow type name, the id and the task queue as strings. The spike compared those, the interval, the overlap policy and the catch-up window.
- **Overlap SKIP.** The poller run took 2.5 s and the interval was 1 s. In 12 s the schedule started 4 runs, skipped 8 ticks (`info.num_actions_skipped_overlap`), and never had more than one `poll_once` running. With `ALLOW_ALL`, it started 11 runs and up to 3 ran at once. The compose server gave the same shape.
- **Run ids.** The schedule appends the start time to the action id, for example `lh201-run-skip-e1393c-2026-10-30T23:52:22Z`.
- **Trigger.** `handle.trigger()` on a paused schedule starts one run, as long as no run is open (an open run makes SKIP drop it). This is what `longhaul poller trigger` needs.
- **Catch-up window.** `SchedulePolicy.catchup_window` defaults to 365 days in the SDK. The spike set 2 minutes. What the schedule does after a long outage was not tested.
- **Server support.** Schedules work on the CLI dev server and on the compose server. They do not work on the time skipping test server.

### Fallbacks

The story names two fallbacks. Neither is needed.

- The pydantic converter exists, so we do not write a custom converter and do not call `model_dump(mode="json")` at boundaries.
- `is_continue_as_new_suggested()` exists, so Continue-As-New uses the turn count and the server suggestion. If a future server never suggests, the turn count alone still works.

### Notes for the stories that use these facts

- **LH-203.** Initialize `self.state` in `@workflow.init`. Call activities by name from `acts.py` and pass `result_type`. Use `REJECT_DUPLICATE` and `FAIL` in `start_task`. Add `httpx2` to the I1 list and `invalid_modules` to the worker. The duplicate start test works in `start_time_skipping()`.
- **LH-204.** Heartbeat from a background task. Never block the event loop in an activity.
- **LH-208.** The worker's `SIGTERM` handler must call the worker's shutdown. The running activity then fails after `graceful_shutdown_timeout` and is retried on the next worker after the retry interval (3.0 s in the spike with a 2 s timeout). Set `graceful_shutdown_timeout` on purpose, because its default is zero.
- **LH-207.** At 6 events per activity, 40 turns stay under 4096 events unless turns average more than 17 activities. A turn has at least four (`check_budget`, `llm_turn`, one `tool_exec`, `append_results`), which is about 24 events. So the turn count fires first in normal runs and the server suggestion is a safety net. Drain handlers with `workflow.all_handlers_finished` before `continue_as_new`.
- **LH-209.** Each kill costs up to the heartbeat timeout of the activity that was running, or about 10 s if it hit the gap between activities. Use short timeouts in the test config so five runs stay fast.
- **LH-307.** Schedule tests need `start_local()` or the compose server. Use the install function above and keep the paused flag.
- **ADR-0002.** The pin table still says `temporalio` is not yet added. It is locked at 1.34.0.
