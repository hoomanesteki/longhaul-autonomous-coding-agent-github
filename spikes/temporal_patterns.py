"""LH-201 spike: check the Temporal Python SDK patterns Phase 2 depends on.

Throwaway code. Each part runs one question from the story for real and prints
what it saw. The findings are written up in docs/adr/0004-temporal.md
(appendix "SDK facts"). Nothing here is imported by the package.

Run from the repo root:

    uv run python spikes/temporal_patterns.py all
    uv run python spikes/temporal_patterns.py sandbox pydantic
    uv run python spikes/temporal_patterns.py heartbeat --address localhost:7233

Parts: sandbox, pydantic, heartbeat, can, timeskip, idreuse, schedule.
Without --address each part starts its own server with WorkflowEnvironment
(the CLI dev server, or the time skipping test server for the timeskip part).
The first start downloads the server binary, so it needs network access.

The heartbeat part runs workers as subprocesses of this script (the internal
"worker" subcommand) so that it can SIGKILL them. LH201_HISTORY_LOOPS shortens
the 900 loop history probe in the can part. The whole run takes about 7 minutes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import logging
import os
import random
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from temporalio import activity, workflow
from temporalio.api.enums.v1 import EventType, WorkflowTaskFailedCause
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleUpdate,
    ScheduleUpdateInput,
    WorkflowHandle,
)
from temporalio.common import RetryPolicy, WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError
from temporalio.runtime import LoggingConfig, Runtime, TelemetryConfig, TelemetryFilter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import (
    SandboxedWorkflowRunner,
    SandboxMatcher,
    SandboxRestrictions,
)

# The contracts module has no I/O and defines the pydantic models that cross the
# workflow boundary. LH201_IMPORT_MODE=plain skips the pass through so the sandbox
# reloads the module, which is what part 1 measures. The default is the pattern
# we plan to use.
_IMPORT_T0 = time.perf_counter()
if os.environ.get("LH201_IMPORT_MODE", "passthrough") == "plain":
    from longhaul.contracts import (
        BlobRef,
        CarryState,
        Outcome,
        OutcomeKind,
        Phase,
        Plan,
        PlanStep,
        ProgressNotes,
        RepoSettings,
        TaskSpec,
        TaskStatus,
    )
else:
    with workflow.unsafe.imports_passed_through():
        from longhaul.contracts import (
            BlobRef,
            CarryState,
            Outcome,
            OutcomeKind,
            Phase,
            Plan,
            PlanStep,
            ProgressNotes,
            RepoSettings,
            TaskSpec,
            TaskStatus,
        )
CONTRACTS_IMPORT_MS = (time.perf_counter() - _IMPORT_T0) * 1000


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------
def say(text: str = "") -> None:
    print(text, flush=True)


def section(title: str) -> None:
    say(f"\n=== {title} ===")


def fact(key: str, value: object) -> None:
    say(f"  {key}: {value}")


def short(err: BaseException, limit: int = 110) -> str:
    text = " ".join(str(err).split())
    return f"{type(err).__name__}: {text[:limit]}"


@asynccontextmanager
async def server(
    address: str | None, extra_args: Sequence[str] = ()
) -> AsyncIterator[tuple[Client, str]]:
    """Yield (client, host:port). Starts a CLI dev server unless an address is given."""
    if address:
        yield await Client.connect(address, data_converter=pydantic_data_converter), address
        return
    env = await WorkflowEnvironment.start_local(
        data_converter=pydantic_data_converter, dev_server_extra_args=list(extra_args)
    )
    try:
        yield env.client, env.client.service_client.config.target_host
    finally:
        await env.shutdown()


def new_queue(prefix: str) -> str:
    return f"lh201-{prefix}-{uuid.uuid4().hex[:8]}"


def make_state(task_id: str = "lh-acme-calc-42") -> CarryState:
    """A CarryState with every nested model filled in and a timezone aware datetime."""
    step = PlanStep(id="s1", description="fix add()", files=["calc.py"], done_when="tests pass")
    return CarryState(
        spec=TaskSpec(
            task_id=task_id,
            repo="acme/calc",
            issue_number=42,
            title="add() subtracts",
            body="add(2, 3) returns -1",
            comments=["keep the public API"],
            base_sha="a" * 40,
            settings=RepoSettings(repo="acme/calc", test_command="pytest -q"),
        ),
        phase=Phase.CODING,
        turn_index=7,
        transcript_ref=BlobRef(key=f"tasks/{task_id}/turns/0007/t.json", sha256="b" * 64, size=99),
        notes=ProgressNotes(
            goal="fix add",
            plan_status=[step],
            files_touched=["calc.py"],
            decisions=["use a one line fix"],
            failing_tests=["test_add"],
            open_hypotheses=[],
            tried_and_failed=[],
            covers_turns_up_to=5,
        ),
        plan=Plan(summary="fix add", steps=[step], acceptance_checks=["pytest -q"]),
        plan_version=1,
        plan_approved=True,
        head_sha="c" * 40,
        sandbox_id="sandbox-1",
        cost_usd=0.1234,
        started_at=datetime(2026, 10, 30, 7, 15, 30, 123456, tzinfo=UTC),
        hints=["prefer pytest"],
    )


# ---------------------------------------------------------------------------
# parts 1 and 2: sandbox rules and the pydantic converter
# ---------------------------------------------------------------------------
RESTRICTED_CALLS: dict[str, Callable[[], Any]] = {
    "datetime.now()": lambda: datetime.now(),  # noqa: DTZ005
    "date.today()": lambda: date.today(),
    "random.random()": lambda: random.random(),
    "random.Random(1).random()": lambda: random.Random(1).random(),
    "time.time()": lambda: time.time(),
    "time.monotonic()": lambda: time.monotonic(),
    "uuid.uuid4()": lambda: uuid.uuid4(),
    "os.getenv('HOME')": lambda: os.getenv("HOME"),
    "os.listdir('.')": lambda: os.listdir("."),
    "Path('.').exists()": lambda: Path(".").exists(),
    "subprocess.run(['true'])": lambda: subprocess.run(["true"], check=False),
    "socket.socket()": lambda: socket.socket(),
    "threading.Thread().start()": lambda: threading.Thread(target=print).start(),
    "workflow.now()": lambda: workflow.now(),
    "workflow.uuid4()": lambda: workflow.uuid4(),
    "workflow.random().random()": lambda: workflow.random().random(),
}


def _import_httpx() -> None:
    import httpx  # noqa: F401


def _import_httpx2() -> None:
    import httpx2  # noqa: F401


def _import_docker() -> None:
    import docker  # noqa: F401


def _import_sqlalchemy() -> None:
    import sqlalchemy  # noqa: F401


def _import_boto3() -> None:
    import boto3  # noqa: F401


def _import_openai() -> None:
    import openai  # noqa: F401


LIBRARY_IMPORTS: dict[str, Callable[[], None]] = {
    "httpx": _import_httpx,
    "httpx2": _import_httpx2,
    "docker": _import_docker,
    "sqlalchemy": _import_sqlalchemy,
    "boto3": _import_boto3,
    "openai": _import_openai,
}


def _verdict(fn: Callable[[], Any]) -> str:
    try:
        value = fn()
    except Exception as err:
        return type(err).__name__
    return f"allowed ({value!r})"[:60]


def _timed_import(fn: Callable[[], None]) -> str:
    with workflow.unsafe.sandbox_unrestricted():
        t0 = time.perf_counter()
    try:
        fn()
        status = "imported"
    except Exception as err:
        status = type(err).__name__
    with workflow.unsafe.sandbox_unrestricted():
        elapsed_ms = (time.perf_counter() - t0) * 1000
    return f"{status} in {elapsed_ms:.0f} ms"


class SandboxReport(BaseModel):
    contracts_import_ms: float
    state_class_id: int
    calls: dict[str, str]
    imports: dict[str, str]
    now_tzinfo: str


@workflow.defn
class SandboxRulesWorkflow:
    @workflow.run
    async def run(self) -> SandboxReport:
        calls = {name: _verdict(fn) for name, fn in RESTRICTED_CALLS.items()}
        imports = {name: _timed_import(fn) for name, fn in LIBRARY_IMPORTS.items()}
        return SandboxReport(
            contracts_import_ms=CONTRACTS_IMPORT_MS,
            state_class_id=id(CarryState),
            calls=calls,
            imports=imports,
            now_tzinfo=str(workflow.now().tzinfo),
        )


def sandbox_runner(*, pass_contracts: bool, block_io_libs: bool) -> SandboxedWorkflowRunner:
    restrictions = SandboxRestrictions.default
    if pass_contracts:
        restrictions = restrictions.with_passthrough_modules("longhaul.contracts")
    if block_io_libs:
        blocked = {
            "httpx",
            "httpx2",
            "docker",
            "sqlalchemy",
            "boto3",
            "botocore",
            "openai",
            "anthropic",
        }
        restrictions = dataclasses.replace(
            restrictions, invalid_modules=SandboxMatcher(access=blocked)
        )
    return SandboxedWorkflowRunner(restrictions=restrictions)


async def run_sandbox_probe(
    client: Client, label: str, runner: SandboxedWorkflowRunner, import_mode: str
) -> SandboxReport:
    os.environ["LH201_IMPORT_MODE"] = import_mode
    queue = new_queue("sandbox")
    async with Worker(
        client, task_queue=queue, workflows=[SandboxRulesWorkflow], workflow_runner=runner
    ):
        report = await client.execute_workflow(
            SandboxRulesWorkflow.run, id=f"lh201-sandbox-{uuid.uuid4().hex[:6]}", task_queue=queue
        )
    same = report.state_class_id == id(CarryState)
    say(f"-- {label}")
    fact("contracts import inside sandbox", f"{report.contracts_import_ms:.1f} ms")
    fact("CarryState is the same class object as outside", same)
    return report


BAD_MODULE = """
import docker  # forbidden by invariant I1
from temporalio import workflow


@workflow.defn
class BadWorkflow:
    @workflow.run
    async def run(self) -> str:
        return "unreachable"
"""


async def forbidden_import_probe(client: Client) -> None:
    """What does a workflow module that imports docker at the top do at worker start?"""
    say("-- E. workflow module with 'import docker' at the top")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "lh201_bad_workflow.py").write_text(BAD_MODULE)
        sys.path.insert(0, tmp)
        try:
            import lh201_bad_workflow  # type: ignore[import-not-found]

            for label, runner in {
                "default sandbox": sandbox_runner(pass_contracts=True, block_io_libs=False),
                "hardened sandbox": sandbox_runner(pass_contracts=True, block_io_libs=True),
            }.items():
                try:
                    Worker(
                        client,
                        task_queue=new_queue("bad"),
                        workflows=[lh201_bad_workflow.BadWorkflow],
                        workflow_runner=runner,
                    )
                    fact(label, "Worker() constructed without error")
                except Exception as err:
                    cause = short(err.__cause__, 150) if err.__cause__ else "no cause"
                    fact(label, f"Worker() raised {short(err, 60)}; cause {cause}")
        finally:
            sys.path.remove(tmp)
            sys.modules.pop("lh201_bad_workflow", None)


@workflow.defn
class UncaughtRestrictedCallWorkflow:
    @workflow.run
    async def run(self) -> str:
        return datetime.now().isoformat()  # noqa: DTZ005  (restricted on purpose)


async def uncaught_restriction_probe(client: Client) -> None:
    say("-- F. restricted call that the workflow does not catch (datetime.now())")
    queue = new_queue("uncaught")
    async with Worker(client, task_queue=queue, workflows=[UncaughtRestrictedCallWorkflow]):
        handle = await client.start_workflow(
            UncaughtRestrictedCallWorkflow.run,
            id=f"lh201-uncaught-{uuid.uuid4().hex[:6]}",
            task_queue=queue,
        )
        await asyncio.sleep(3.0)
        status = (await handle.describe()).status
        failures = [
            event.workflow_task_failed_event_attributes
            async for event in handle.fetch_history_events()
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED
        ]
        await handle.terminate()
    fact("workflow status after 3 s", status.name if status else None)
    fact("failed workflow tasks in 3 s (the SDK keeps retrying)", len(failures))
    if failures:
        fact("cause of the first failure", WorkflowTaskFailedCause.Name(failures[0].cause))
        fact("message", " ".join(failures[0].failure.message.split())[:150])


async def part_sandbox(address: str | None) -> None:
    section("1. Workflow sandbox import rules")
    async with server(address) as (client, _):
        plain = await run_sandbox_probe(
            client,
            "A. default sandbox, no pass through (module reloaded for every workflow run)",
            sandbox_runner(pass_contracts=False, block_io_libs=False),
            "plain",
        )
        await run_sandbox_probe(
            client,
            "B. imports_passed_through() around the contracts import (planned pattern)",
            sandbox_runner(pass_contracts=False, block_io_libs=False),
            "passthrough",
        )
        await run_sandbox_probe(
            client,
            "C. worker level SandboxRestrictions.with_passthrough_modules('longhaul.contracts')",
            sandbox_runner(pass_contracts=True, block_io_libs=False),
            "plain",
        )
        say("-- calls inside workflow code (default restrictions)")
        for name, verdict in plain.calls.items():
            fact(name, verdict)
        fact("workflow.now().tzinfo", plain.now_tzinfo)
        say("-- importing I/O libraries inside workflow code (default restrictions)")
        for name, verdict in plain.imports.items():
            fact(f"import {name}", verdict)
        hardened = await run_sandbox_probe(
            client,
            "D. hardened: invalid_modules blocks the I/O libraries from invariant I1",
            sandbox_runner(pass_contracts=True, block_io_libs=True),
            "passthrough",
        )
        for name, verdict in hardened.imports.items():
            fact(f"import {name}", verdict)
        await forbidden_import_probe(client)
        await uncaught_restriction_probe(client)


# --- part 2: pydantic data converter ---------------------------------------
@activity.defn
async def echo_state(state: CarryState) -> CarryState:
    """Decodes and encodes outside the sandbox, so both sides of the converter run."""
    return state.model_copy(update={"turn_index": state.turn_index + 1})


@workflow.defn
class RoundTripWorkflow:
    def __init__(self) -> None:
        self.state: CarryState | None = None
        self.hints: list[str] = []
        self.budget: list[tuple[str, float, int]] = []

    @workflow.run
    async def run(self, state: CarryState) -> CarryState:
        self.state = state
        if workflow.info().continued_run_id is None:
            echoed = await workflow.execute_activity(
                echo_state, state, start_to_close_timeout=timedelta(seconds=10)
            )
            await workflow.wait_condition(lambda: bool(self.hints) and bool(self.budget))
            echoed.hints.extend(self.hints)
            echoed.cost_usd += sum(usd for _, usd, _ in self.budget)
            workflow.continue_as_new(echoed)
        return state.model_copy(update={"phase": Phase.DONE})

    @workflow.signal
    def hint(self, by: str, text: str) -> None:
        self.hints.append(f"{by}: {text}")

    @workflow.signal
    def add_budget(self, by: str, usd: float, turns: int) -> None:
        self.budget.append((by, usd, turns))

    @workflow.query
    def status(self) -> TaskStatus:
        assert self.state is not None
        return TaskStatus(
            task_id=self.state.spec.task_id,
            phase=self.state.phase,
            turn_index=self.state.turn_index,
            cost_usd=self.state.cost_usd,
            last_checkpoint=self.state.head_sha,
            plan_version=self.state.plan_version,
            outcome=Outcome(kind=OutcomeKind.PR_OPENED, pr_url=None, summary="probe"),
        )


@workflow.defn
class ByNameWorkflow:
    """The plan keeps activity stubs in acts.py and calls activities by name."""

    @workflow.run
    async def run(self, state: CarryState) -> dict[str, str]:
        timeout = timedelta(seconds=10)
        typed = await workflow.execute_activity(
            "echo_state", state, result_type=CarryState, start_to_close_timeout=timeout
        )
        untyped = await workflow.execute_activity(
            "echo_state", state, start_to_close_timeout=timeout
        )
        return {"with result_type": type(typed).__name__, "without": type(untyped).__name__}


def offline_converter_checks() -> None:
    say("-- converters without a server")
    state = make_state()
    payloads = pydantic_data_converter.payload_converter.to_payloads([state])
    raw = payloads[0].data.decode()
    fact("pydantic converter encoding", payloads[0].metadata["encoding"].decode())
    fact("payload bytes for CarryState", len(payloads[0].data))
    fact("started_at as JSON", raw[raw.index('"started_at"') :][:44])
    back = pydantic_data_converter.payload_converter.from_payloads(payloads, [CarryState])[0]
    fact("typed round trip equal", back == state)
    fact("started_at tzinfo after decode", repr(back.started_at.tzinfo))
    naive = state.model_copy(update={"started_at": datetime(2026, 10, 30, 7, 15, 30)})
    naive_payload = pydantic_data_converter.payload_converter.to_payloads([naive])
    naive_back = pydantic_data_converter.payload_converter.from_payloads(
        naive_payload, [CarryState]
    )
    fact("naive datetime stays naive", naive_back[0].started_at.tzinfo is None)
    untyped = pydantic_data_converter.payload_converter.from_payloads(payloads, None)[0]
    fact("decode without a type hint returns", type(untyped).__name__)
    try:
        default_payloads = DataConverter.default.payload_converter.to_payloads([state])
        default_back = DataConverter.default.payload_converter.from_payloads(
            default_payloads, [CarryState]
        )[0]
        fact("default converter typed decode returns", type(default_back).__name__)
        fact("default converter result equal", default_back == state)
    except Exception as err:
        fact("default converter with a pydantic model", short(err, 140))


async def run_round_trip(client: Client, label: str, import_mode: str) -> None:
    os.environ["LH201_IMPORT_MODE"] = import_mode
    runner = sandbox_runner(pass_contracts=False, block_io_libs=False)
    queue = new_queue("rt")
    state = make_state(f"lh-rt-{uuid.uuid4().hex[:6]}")
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[RoundTripWorkflow, ByNameWorkflow],
            activities=[echo_state],
            workflow_runner=runner,
        ):
            handle = await client.start_workflow(
                RoundTripWorkflow.run, state, id=state.spec.task_id, task_queue=queue
            )
            await handle.signal(RoundTripWorkflow.hint, args=["alice", "try the other test"])
            await handle.signal(RoundTripWorkflow.add_budget, args=["alice", 0.5, 10])
            status = await handle.query(RoundTripWorkflow.status)
            result = await asyncio.wait_for(handle.result(), 30)
            by_name = await client.execute_workflow(
                ByNameWorkflow.run, state, id=f"{state.spec.task_id}-by-name", task_queue=queue
            )
    except Exception as err:
        say(f"-- {label}")
        fact("failed", short(err, 200))
        return
    expected = state.model_copy(
        update={
            "turn_index": state.turn_index + 1,
            "hints": [*state.hints, "alice: try the other test"],
            "cost_usd": state.cost_usd + 0.5,
            "phase": Phase.DONE,
        }
    )
    say(f"-- {label}")
    fact("result type", type(result).__name__)
    fact("result equals expected (after activity + continue_as_new)", result == expected)
    fact(
        "started_at identical, tzinfo",
        f"{result.started_at == state.started_at}, {result.started_at.tzinfo!r}",
    )
    fact(
        "query returns", f"{type(status).__name__} phase={status.phase!r} outcome={status.outcome}"
    )
    fact("activity called by name, type of its result", by_name)


async def part_pydantic(address: str | None) -> None:
    section("2. Pydantic data converter")
    import temporalio.contrib.pydantic as contrib

    fact("temporalio.contrib.pydantic present", contrib.__file__)
    fact("exports", sorted(n for n in dir(contrib) if "ydantic" in n or n == "ToJsonOptions"))
    offline_converter_checks()
    async with server(address) as (client, _):
        await run_round_trip(client, "workflow round trip, contracts passed through", "passthrough")
        await run_round_trip(client, "workflow round trip, contracts reloaded in sandbox", "plain")


# ---------------------------------------------------------------------------
# part 3: heartbeats and heartbeat_timeout when the worker dies
# ---------------------------------------------------------------------------
class HbArgs(BaseModel):
    steps: int = 10
    interval_s: float = 1.0
    heartbeat_timeout_s: float | None = 5.0
    start_to_close_s: float = 120.0
    mode: str = "direct"  # direct | blocked


class HbResult(BaseModel):
    attempt: int
    started_wall: float
    resumed_from: int
    finished_wall: float
    worker_pid: int


@activity.defn
async def heartbeating_activity(args: HbArgs) -> HbResult:
    info = activity.info()
    started = time.time()
    resumed_from = int(info.heartbeat_details[0]) if info.heartbeat_details else 0
    if args.mode == "blocked":
        # The pattern planned for llm_turn: a background task heartbeats while the body waits.
        # Attempt 1 then blocks the event loop with a synchronous call, as a sync SDK would.
        async def beat() -> None:
            while True:
                activity.heartbeat(0)
                await asyncio.sleep(1.0)

        beater = asyncio.create_task(beat())
        try:
            if info.attempt == 1:
                time.sleep(args.steps * args.interval_s)
            else:
                await asyncio.sleep(0.2)
        finally:
            beater.cancel()
    else:
        for step in range(resumed_from + 1, args.steps + 1):
            await asyncio.sleep(args.interval_s)
            activity.heartbeat(step)
    return HbResult(
        attempt=info.attempt,
        started_wall=started,
        resumed_from=resumed_from,
        finished_wall=time.time(),
        worker_pid=os.getpid(),
    )


@workflow.defn
class HeartbeatWorkflow:
    @workflow.run
    async def run(self, args: HbArgs) -> HbResult:
        timeout = args.heartbeat_timeout_s
        return await workflow.execute_activity(
            heartbeating_activity,
            args,
            start_to_close_timeout=timedelta(seconds=args.start_to_close_s),
            heartbeat_timeout=timedelta(seconds=timeout) if timeout else None,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1), backoff_coefficient=1.0, maximum_attempts=3
            ),
        )


@workflow.defn
class TimerWorkflow:
    """No activity at all: the workflow only waits on a timer."""

    @workflow.run
    async def run(self, seconds: float) -> str:
        await workflow.sleep(timedelta(seconds=seconds))
        return "timer fired"


async def worker_process(address: str, queue: str, ready_file: str, graceful_s: float) -> None:
    """Body of the worker subprocess. SIGTERM shuts it down gracefully; SIGKILL cannot be caught."""
    client = await Client.connect(address, data_converter=pydantic_data_converter)
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    async with Worker(
        client,
        task_queue=queue,
        workflows=[HeartbeatWorkflow, TimerWorkflow],
        activities=[heartbeating_activity],
        graceful_shutdown_timeout=timedelta(seconds=graceful_s),
    ):
        Path(ready_file).write_text("ready")
        await stop.wait()


def worker_main(argv: Sequence[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--ready-file", required=True)
    parser.add_argument("--graceful-seconds", type=float, default=0.0)
    args = parser.parse_args(argv)
    asyncio.run(worker_process(args.address, args.queue, args.ready_file, args.graceful_seconds))


def spawn_worker(
    address: str, queue: str, ready: Path, graceful_s: float
) -> subprocess.Popen[bytes]:
    cmd = [sys.executable, __file__, "worker", "--address", address, "--queue", queue]
    cmd += ["--ready-file", str(ready), "--graceful-seconds", str(graceful_s)]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


async def wait_for(predicate: Callable[[], bool], timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


def _epoch(ts: Any) -> float | None:
    return ts.ToNanoseconds() / 1e9 if ts.ToNanoseconds() else None


async def pending_activity(handle: WorkflowHandle[Any, Any]) -> dict[str, Any] | None:
    desc = await handle.describe()
    if not desc.raw_description.pending_activities:
        return None
    pa = desc.raw_description.pending_activities[0]
    failure = pa.last_failure
    return {
        "attempt": pa.attempt,
        "state": pa.state,
        "last_heartbeat": _epoch(pa.last_heartbeat_time),
        "last_started": _epoch(pa.last_started_time),
        "failure": failure.message if failure.message else None,
        "timeout_type": failure.timeout_failure_info.timeout_type if failure.message else None,
    }


async def watch_attempts(
    handle: WorkflowHandle[Any, Any], seen: list[tuple[float, int, int, str | None]]
) -> None:
    """Poll describe every 100 ms and keep each change of (attempt, state)."""
    last: tuple[int, int] | None = None
    while True:
        info = await pending_activity(handle)
        if info and (info["attempt"], info["state"]) != last:
            last = (info["attempt"], info["state"])
            seen.append((time.time(), info["attempt"], info["state"], info["failure"]))
        await asyncio.sleep(0.1)


async def heartbeat_trial(
    client: Client, address: str, label: str, args: HbArgs, how: str, graceful_s: float = 0.0
) -> None:
    say(f"-- {label}")
    queue = new_queue("hb")
    with tempfile.TemporaryDirectory() as tmp:
        ready1, ready2 = Path(tmp) / "ready1", Path(tmp) / "ready2"
        first = spawn_worker(address, queue, ready1, graceful_s)
        second: subprocess.Popen[bytes] | None = None
        try:
            await wait_for(ready1.exists, 60, "first worker")
            handle = await client.start_workflow(
                HeartbeatWorkflow.run, args, id=f"lh201-hb-{uuid.uuid4().hex[:6]}", task_queue=queue
            )
            seen: list[tuple[float, int, int, str | None]] = []
            watcher = asyncio.create_task(watch_attempts(handle, seen))
            t_start = time.time()
            if how == "none":
                result = await asyncio.wait_for(handle.result(), 60)
            else:
                if args.mode == "direct":
                    while True:
                        info = await pending_activity(handle)
                        if info and info["last_heartbeat"]:
                            break
                        await asyncio.sleep(0.1)
                await asyncio.sleep(3.0)
                info = await pending_activity(handle)
                last_hb = info["last_heartbeat"] if info else None
                t_signal = time.time()
                first.send_signal(signal.SIGKILL if how == "kill" else signal.SIGTERM)
                second = spawn_worker(address, queue, ready2, graceful_s)
                await asyncio.to_thread(first.wait)
                t_exit = time.time()
                result = await asyncio.wait_for(handle.result(), 120)
                fact("attempt 1 start to signal", f"{t_signal - t_start:.1f} s")
                if last_hb:
                    fact(
                        "last heartbeat seen by server, before signal",
                        f"{t_signal - last_hb:.1f} s",
                    )
                fact("worker process exited", f"{t_exit - t_signal:.1f} s after the signal")
                fact("signal to attempt 2 start", f"{result.started_wall - t_signal:.1f} s")
            watcher.cancel()
            fact("activity result attempt", result.attempt)
            fact("attempt 2 resumed from heartbeat detail", result.resumed_from)
            fact("attempt 2 ran in a different process", result.worker_pid != first.pid)
            changes = [
                f"+{t - t_start:.1f}s attempt={a} state={st} failure={f!r}"[:100]
                for t, a, st, f in seen
            ]
            fact("pending activity changes seen by describe()", changes)
        finally:
            for proc in (first, second):
                if proc and proc.poll() is None:
                    proc.kill()
                    proc.wait()


async def timer_kill_trial(client: Client, address: str) -> None:
    """Kill the worker between activities: the workflow only waits for a timer."""
    say("-- SIGKILL the worker while the workflow only waits on a 4 s timer")
    queue = new_queue("timer")
    with tempfile.TemporaryDirectory() as tmp:
        ready1, ready2 = Path(tmp) / "ready1", Path(tmp) / "ready2"
        first = spawn_worker(address, queue, ready1, 0.0)
        second: subprocess.Popen[bytes] | None = None
        try:
            await wait_for(ready1.exists, 60, "first worker")
            t_start = time.time()
            handle = await client.start_workflow(
                TimerWorkflow.run, 4.0, id=f"lh201-timer-{uuid.uuid4().hex[:6]}", task_queue=queue
            )
            await asyncio.sleep(1.0)
            first.send_signal(signal.SIGKILL)
            t_kill = time.time()
            second = spawn_worker(address, queue, ready2, 0.0)
            await asyncio.wait_for(handle.result(), 90)
            t_done = time.time()
            fact("timer was due", f"{4.0 - (t_kill - t_start):.1f} s after the kill")
            fact("workflow finished", f"{t_done - t_kill:.1f} s after the kill")
            fact("extra delay caused by the dead worker", f"{(t_done - t_start) - 4.0:.1f} s")
        finally:
            for proc in (first, second):
                if proc and proc.poll() is None:
                    proc.kill()
                    proc.wait()


async def part_heartbeat(address: str | None) -> None:
    section("3. Activity heartbeats when the worker dies")
    async with server(address) as (client, target):
        await heartbeat_trial(
            client,
            target,
            "SIGKILL the worker, heartbeat_timeout=3 s",
            HbArgs(heartbeat_timeout_s=3),
            "kill",
        )
        await heartbeat_trial(
            client,
            target,
            "SIGKILL the worker, heartbeat_timeout=15 s (the tool_exec value)",
            HbArgs(heartbeat_timeout_s=15),
            "kill",
        )
        await heartbeat_trial(
            client,
            target,
            "SIGKILL the worker, no heartbeat_timeout, start_to_close=20 s",
            HbArgs(heartbeat_timeout_s=None, start_to_close_s=20),
            "kill",
        )
        await heartbeat_trial(
            client,
            target,
            "SIGTERM the worker, graceful_shutdown_timeout=2 s, heartbeat_timeout=15 s",
            HbArgs(heartbeat_timeout_s=15),
            "term",
            graceful_s=2.0,
        )
        await heartbeat_trial(
            client,
            target,
            "worker alive but event loop blocked, heartbeat_timeout=3 s",
            HbArgs(heartbeat_timeout_s=3, steps=6, mode="blocked"),
            "none",
        )
        await timer_kill_trial(client, target)


# ---------------------------------------------------------------------------
# part 4: Continue-As-New, history length and the server suggestion
# ---------------------------------------------------------------------------
class HistoryReport(BaseModel):
    iterations: int
    suggested_at_iteration: int | None
    length_at_suggestion: int | None
    size_at_suggestion: int | None
    length_samples: dict[int, int]


class CanState(BaseModel):
    loop: int = 0
    max_loops: int = 12
    every: int = 0  # also continue as new every N loops; 0 means only on the server suggestion
    delay_s: float = 0.0
    runs: int = 1
    hints: list[str] = []
    log: list[str] = []
    # True loads the carried hints inside run() instead of @workflow.init, to show the pitfall.
    load_in_run: bool = False


@activity.defn
async def noop_activity(delay_s: float = 0.0) -> None:
    if delay_s:
        await asyncio.sleep(delay_s)


@workflow.defn
class HistoryProbeWorkflow:
    """One activity per iteration, as the turn loop will do. Stops at the first suggestion."""

    @workflow.run
    async def run(self, max_iterations: int) -> HistoryReport:
        samples: dict[int, int] = {}
        for i in range(1, max_iterations + 1):
            await workflow.execute_activity(
                noop_activity, 0.0, start_to_close_timeout=timedelta(seconds=10)
            )
            info = workflow.info()
            if i in (1, 2, 10, 100):
                samples[i] = info.get_current_history_length()
            if info.is_continue_as_new_suggested():
                return HistoryReport(
                    iterations=i,
                    suggested_at_iteration=i,
                    length_at_suggestion=info.get_current_history_length(),
                    size_at_suggestion=info.get_current_history_size(),
                    length_samples=samples,
                )
        return HistoryReport(
            iterations=max_iterations,
            suggested_at_iteration=None,
            length_at_suggestion=None,
            size_at_suggestion=None,
            length_samples=samples,
        )


@workflow.defn
class CanChainWorkflow:
    """Continues as new on the server suggestion or every N loops, carrying its state."""

    # @workflow.init runs before any signal handler, even for signals that were delivered in
    # the same first workflow task as the start event.
    @workflow.init
    def __init__(self, st: CanState) -> None:
        self.hints: list[str] = [] if st.load_in_run else list(st.hints)

    @workflow.signal
    def hint(self, text: str) -> None:
        self.hints.append(text)

    @workflow.run
    async def run(self, st: CanState) -> CanState:
        if st.load_in_run:
            self.hints = list(st.hints)  # drops signals that arrived before run() started
        while st.loop < st.max_loops:
            await workflow.execute_activity(
                noop_activity, st.delay_s, start_to_close_timeout=timedelta(seconds=10)
            )
            st.loop += 1
            info = workflow.info()
            suggested = info.is_continue_as_new_suggested()
            due = st.every > 0 and st.loop % st.every == 0
            if (suggested or due) and st.loop < st.max_loops:
                st.log.append(
                    f"run {st.runs} ended at loop {st.loop}: "
                    f"history_length={info.get_current_history_length()} suggested={suggested}"
                )
                # Let running signal handlers finish, then move their effects into the state.
                await workflow.wait_condition(workflow.all_handlers_finished)
                st.hints = list(self.hints)
                st.runs += 1
                workflow.continue_as_new(st)
        st.hints = list(self.hints)
        return st


async def history_suggestion_probe(client: Client, label: str, iterations: int) -> None:
    queue = new_queue("hist")
    async with Worker(
        client, task_queue=queue, workflows=[HistoryProbeWorkflow], activities=[noop_activity]
    ):
        t0 = time.monotonic()
        report = await client.execute_workflow(
            HistoryProbeWorkflow.run,
            iterations,
            id=f"lh201-hist-{uuid.uuid4().hex[:6]}",
            task_queue=queue,
            execution_timeout=timedelta(minutes=5),
        )
        took = time.monotonic() - t0
    say(f"-- {label}")
    fact("history length after iteration 1, 2, 10, 100", report.length_samples)
    if report.suggested_at_iteration is None:
        fact("is_continue_as_new_suggested()", f"never True in {report.iterations} iterations")
    else:
        fact(
            "is_continue_as_new_suggested() first True at iteration", report.suggested_at_iteration
        )
        fact(
            "history length / size then",
            f"{report.length_at_suggestion} events / {report.size_at_suggestion} bytes",
        )
    fact("loop wall time", f"{took:.1f} s")


async def can_chain_probe(client: Client, label: str, state: CanState) -> CanState:
    queue = new_queue("can")
    async with Worker(
        client, task_queue=queue, workflows=[CanChainWorkflow], activities=[noop_activity]
    ):
        result = await client.execute_workflow(
            CanChainWorkflow.run,
            state,
            id=f"lh201-can-{uuid.uuid4().hex[:6]}",
            task_queue=queue,
            execution_timeout=timedelta(minutes=2),
        )
    say(f"-- {label}")
    fact("runs in the chain", result.runs)
    for line in result.log:
        fact("  ", line)
    return result


async def signals_during_can_probe(client: Client, *, load_in_run: bool) -> None:
    queue = new_queue("sig")
    wf_id = f"lh201-sig-{uuid.uuid4().hex[:6]}"
    state = CanState(max_loops=60, every=3, delay_s=0.02, load_in_run=load_in_run)
    accepted = 0
    rejected: list[str] = []
    async with Worker(
        client, task_queue=queue, workflows=[CanChainWorkflow], activities=[noop_activity]
    ):
        handle = await client.start_workflow(
            CanChainWorkflow.run, state, id=wf_id, task_queue=queue
        )
        sender = client.get_workflow_handle(wf_id)  # no run id: always the current run
        sent = 0
        while True:
            try:
                await sender.signal(CanChainWorkflow.hint, f"h{sent}")
                accepted += 1
            except Exception as err:
                rejected.append(short(err, 80))
                break
            sent += 1
            await asyncio.sleep(0.005)
        result = await handle.result(follow_runs=True)
    failed_causes: dict[str, int] = {}
    executions = 0
    async for execution in client.list_workflows(f"WorkflowId = '{wf_id}'"):
        executions += 1
        history = client.get_workflow_handle(wf_id, run_id=execution.run_id)
        async for event in history.fetch_history_events():
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED:
                cause = event.workflow_task_failed_event_attributes.cause
                name = WorkflowTaskFailedCause.Name(cause)
                failed_causes[name] = failed_causes.get(name, 0) + 1
    where = "in run() (the pitfall)" if load_in_run else "in @workflow.init"
    say(f"-- signals every 5 ms, 19 continue-as-new hops, carried state loaded {where}")
    fact(
        "runs in the chain (workflow's own count / listed by the server)",
        f"{result.runs} / {executions}",
    )
    fact("signals accepted by the server", accepted)
    fact("hints the workflow ended up with", len(result.hints))
    fact("no hint lost or duplicated", result.hints == [f"h{i}" for i in range(accepted)])
    fact("signal calls rejected, after the workflow closed", rejected[:1])
    fact("WorkflowTaskFailed events across all runs, by cause", failed_causes or "none")


async def part_can(address: str | None) -> None:
    section("4. Continue-As-New: is_continue_as_new_suggested() and history length")
    wanted = (
        "is_continue_as_new_suggested",
        "get_current_history_length",
        "get_current_history_size",
    )
    fact("workflow.Info has", [name for name in wanted if hasattr(workflow.Info, name)])
    fact("ContinueAsNewError derives from", workflow.ContinueAsNewError.__mro__[1].__name__)
    fact("workflow.all_handlers_finished exists", hasattr(workflow, "all_handlers_finished"))
    async with server(address) as (client, _):
        iterations = int(os.environ.get("LH201_HISTORY_LOOPS", "900"))  # 683 are enough
        await history_suggestion_probe(
            client, "default server settings, one activity per loop", iterations
        )
        await signals_during_can_probe(client, load_in_run=True)
        await signals_during_can_probe(client, load_in_run=False)
    if address:
        return
    low = ["--dynamic-config-value", "limit.historyCount.suggestContinueAsNew=40"]
    async with server(None, extra_args=low) as (client, _):
        await can_chain_probe(
            client,
            "server threshold lowered to 40 events, continue as new only on the suggestion",
            CanState(max_loops=20),
        )


# ---------------------------------------------------------------------------
# part 5: time skipping
# ---------------------------------------------------------------------------
@workflow.defn
class HintWaitWorkflow:
    def __init__(self) -> None:
        self.hint: str | None = None

    @workflow.signal
    def send_hint(self, text: str) -> None:
        self.hint = text

    @workflow.run
    async def run(self, wait_s: float) -> str:
        start = workflow.now()
        try:
            await workflow.wait_condition(
                lambda: self.hint is not None, timeout=timedelta(seconds=wait_s)
            )
            outcome = f"hint {self.hint!r}"
        except TimeoutError:
            outcome = "timed out"
        return f"{outcome} after {workflow.now() - start}"


@activity.defn
async def fail_first_attempt(label: str) -> int:
    attempt = activity.info().attempt
    if attempt == 1:
        raise ApplicationError(f"{label}: first attempt fails")
    return attempt


@workflow.defn
class BackoffWorkflow:
    @workflow.run
    async def run(self) -> str:
        start = workflow.now()
        attempt = await workflow.execute_activity(
            fail_first_attempt,
            "backoff",
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(initial_interval=timedelta(minutes=10), maximum_attempts=3),
        )
        return f"succeeded on attempt {attempt} after {workflow.now() - start}"


@activity.defn
async def slow_activity(seconds: float) -> int:
    await asyncio.sleep(seconds)
    return activity.info().attempt


@workflow.defn
class SlowActivityWorkflow:
    @workflow.run
    async def run(self, seconds: float) -> str:
        start = workflow.now()
        attempt = await workflow.execute_activity(
            slow_activity,
            seconds,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        return f"attempt {attempt}, workflow clock advanced {workflow.now() - start}"


async def part_timeskip(address: str | None) -> None:
    section("5. WorkflowEnvironment.start_time_skipping()")
    env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    try:
        client = env.client
        fact("supports_time_skipping", env.supports_time_skipping)
        queue = new_queue("ts")
        async with Worker(
            client,
            task_queue=queue,
            workflows=[HintWaitWorkflow, BackoffWorkflow, SlowActivityWorkflow],
            activities=[fail_first_attempt, slow_activity],
        ):
            wall, sim = time.monotonic(), await env.get_current_time()
            out = await client.execute_workflow(
                HintWaitWorkflow.run,
                24 * 3600.0,
                id=f"lh201-ts-{uuid.uuid4().hex[:6]}",
                task_queue=queue,
            )
            fact("24 h wait_condition timeout, awaiting the result", out)
            fact("  wall time", f"{time.monotonic() - wall:.2f} s")

            handle = await client.start_workflow(
                HintWaitWorkflow.run,
                3600.0,
                id=f"lh201-ts-{uuid.uuid4().hex[:6]}",
                task_queue=queue,
            )
            sim = await env.get_current_time()
            await asyncio.sleep(1.5)
            status = (await handle.describe()).status
            fact(
                "1.5 s real sleep in the test, not awaiting the result",
                f"status {status.name if status else None}, "
                f"test clock advance {await env.get_current_time() - sim}",
            )
            await handle.signal(HintWaitWorkflow.send_hint, "use pytest")
            fact("  then signal and await", await handle.result())

            sim = await env.get_current_time()
            await env.sleep(timedelta(hours=2))
            fact("env.sleep(2 h) moves the test clock by", await env.get_current_time() - sim)

            wall, sim = time.monotonic(), await env.get_current_time()
            out = await client.execute_workflow(
                BackoffWorkflow.run, id=f"lh201-ts-{uuid.uuid4().hex[:6]}", task_queue=queue
            )
            fact("activity retry backoff of 10 min", out)
            fact("  wall time", f"{time.monotonic() - wall:.2f} s")

            # A real 3 s activity with a 10 s heartbeat timeout and no heartbeats. If the test
            # server skipped time while the activity ran, this would time out.
            wall = time.monotonic()
            try:
                out = await client.execute_workflow(
                    SlowActivityWorkflow.run,
                    3.0,
                    id=f"lh201-ts-{uuid.uuid4().hex[:6]}",
                    task_queue=queue,
                )
            except Exception as err:
                out = short(err.__cause__ or err, 120)
            fact("3 s real activity, heartbeat_timeout=10 s, no heartbeats", out)
            fact("  wall time", f"{time.monotonic() - wall:.2f} s")

            ahead = await env.get_current_time() - datetime.now(UTC)
            fact("test clock minus real clock after these runs", ahead)

            with env.auto_time_skipping_disabled():  # a plain context manager, not an async one
                wall = time.monotonic()
                out = await client.execute_workflow(
                    HintWaitWorkflow.run,
                    2.0,
                    id=f"lh201-ts-{uuid.uuid4().hex[:6]}",
                    task_queue=queue,
                )
                fact(
                    "auto_time_skipping_disabled(), 2 s timer",
                    f"{out}; wall {time.monotonic() - wall:.1f} s",
                )

        say("-- what the time skipping test server does not do")
        await history_suggestion_probe(
            client, "history suggestion on the time skipping server", 900
        )
        await id_policy_quick_check(client, "time_skipping")
        await schedule_support_check(client)
    finally:
        await env.shutdown()


# ---------------------------------------------------------------------------
# part 6: starting a workflow whose id already exists
# ---------------------------------------------------------------------------
@workflow.defn
class HoldWorkflow:
    def __init__(self) -> None:
        self.finish_now = False
        self.explode_now = False

    @workflow.signal
    def finish(self) -> None:
        self.finish_now = True

    @workflow.signal
    def explode(self) -> None:
        self.explode_now = True

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.finish_now or self.explode_now)
        if self.explode_now:
            raise ApplicationError("boom", non_retryable=True)
        return "done"


REUSE = WorkflowIDReusePolicy
CONFLICT = WorkflowIDConflictPolicy


async def try_start(
    client: Client, queue: str, wf_id: str, first_run: str | None, **policies: Any
) -> str:
    try:
        handle = await client.start_workflow(
            HoldWorkflow.run, id=wf_id, task_queue=queue, **policies
        )
    except Exception as err:
        text = f"{type(err).__module__}.{type(err).__name__}: {err}"
        if isinstance(err, WorkflowAlreadyStartedError):
            same = err.run_id == first_run if first_run else None
            text += f" [workflow_id={err.workflow_id}, workflow_type={err.workflow_type}, "
            text += f"run_id is the existing run: {same}]"
        return text
    same = handle.result_run_id == first_run
    return f"started, handle points at {'the existing run' if same else 'a new run'}"


async def id_policy_matrix(client: Client, label: str) -> None:
    queue = new_queue("id")
    say(f"-- {label}: the existing run is RUNNING")
    running_cases: list[tuple[str, dict[str, Any]]] = [
        ("SDK defaults", {}),
        ("conflict=FAIL", {"id_conflict_policy": CONFLICT.FAIL}),
        ("reuse=REJECT_DUPLICATE", {"id_reuse_policy": REUSE.REJECT_DUPLICATE}),
        (
            "reuse=REJECT_DUPLICATE conflict=FAIL",
            {"id_reuse_policy": REUSE.REJECT_DUPLICATE, "id_conflict_policy": CONFLICT.FAIL},
        ),
        ("conflict=USE_EXISTING", {"id_conflict_policy": CONFLICT.USE_EXISTING}),
        (
            "reuse=REJECT_DUPLICATE conflict=USE_EXISTING",
            {
                "id_reuse_policy": REUSE.REJECT_DUPLICATE,
                "id_conflict_policy": CONFLICT.USE_EXISTING,
            },
        ),
        ("conflict=TERMINATE_EXISTING", {"id_conflict_policy": CONFLICT.TERMINATE_EXISTING}),
        ("reuse=TERMINATE_IF_RUNNING", {"id_reuse_policy": REUSE.TERMINATE_IF_RUNNING}),
    ]
    async with Worker(client, task_queue=queue, workflows=[HoldWorkflow]):
        for name, policies in running_cases:
            wf_id = f"lh201-id-{uuid.uuid4().hex[:8]}"
            first = await client.start_workflow(HoldWorkflow.run, id=wf_id, task_queue=queue)
            fact(name, await try_start(client, queue, wf_id, first.result_run_id, **policies))
            await client.get_workflow_handle(wf_id).terminate()

        say(f"-- {label}: the existing run is CLOSED")
        closers = {
            "completed": lambda h: h.signal(HoldWorkflow.finish),
            "failed": lambda h: h.signal(HoldWorkflow.explode),
            "terminated": lambda h: h.terminate(),
        }
        closed_cases: list[tuple[str, dict[str, Any]]] = [
            ("SDK defaults", {}),
            (
                "reuse=ALLOW_DUPLICATE_FAILED_ONLY",
                {"id_reuse_policy": REUSE.ALLOW_DUPLICATE_FAILED_ONLY},
            ),
            ("reuse=REJECT_DUPLICATE", {"id_reuse_policy": REUSE.REJECT_DUPLICATE}),
        ]
        for closed_as, close in closers.items():
            for name, policies in closed_cases:
                wf_id = f"lh201-id-{uuid.uuid4().hex[:8]}"
                first = await client.start_workflow(HoldWorkflow.run, id=wf_id, task_queue=queue)
                await close(first)
                with contextlib.suppress(Exception):
                    await first.result()
                fact(
                    f"{closed_as}, {name}",
                    await try_start(client, queue, wf_id, first.result_run_id, **policies),
                )
                with contextlib.suppress(Exception):
                    await client.get_workflow_handle(wf_id).terminate()


async def id_policy_quick_check(client: Client, label: str) -> None:
    queue = new_queue("idq")
    async with Worker(client, task_queue=queue, workflows=[HoldWorkflow]):
        wf_id = f"lh201-idq-{uuid.uuid4().hex[:8]}"
        first = await client.start_workflow(HoldWorkflow.run, id=wf_id, task_queue=queue)
        for name, policies in (
            ("defaults", {}),
            (
                "reuse=REJECT_DUPLICATE conflict=FAIL",
                {"id_reuse_policy": REUSE.REJECT_DUPLICATE, "id_conflict_policy": CONFLICT.FAIL},
            ),
            ("conflict=USE_EXISTING", {"id_conflict_policy": CONFLICT.USE_EXISTING}),
        ):
            fact(
                f"{label}: duplicate start with {name}",
                await try_start(client, queue, wf_id, first.result_run_id, **policies),
            )
        await first.terminate()


async def part_idreuse(address: str | None) -> None:
    section("6. Starting a workflow with an id that already exists")
    async with server(address) as (client, _):
        ns = await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=client.namespace)
        )
        retention = ns.config.workflow_execution_retention_ttl.ToTimedelta()
        fact("namespace retention (REJECT_DUPLICATE only lasts this long)", retention)
        await id_policy_matrix(client, "CLI dev server")


# ---------------------------------------------------------------------------
# part 7: Schedules for the poller
# ---------------------------------------------------------------------------
class PollStats:
    """Counts overlapping poll_once calls. The worker runs in this process, so this is exact."""

    active = 0
    peak = 0


@activity.defn
async def poll_once(work_s: float) -> int:
    PollStats.active += 1
    PollStats.peak = max(PollStats.peak, PollStats.active)
    try:
        await asyncio.sleep(work_s)
    finally:
        PollStats.active -= 1
    return 0


@workflow.defn
class PollerWorkflow:
    @workflow.run
    async def run(self, work_s: float) -> str:
        await workflow.execute_activity(
            poll_once, work_s, start_to_close_timeout=timedelta(seconds=30)
        )
        return "polled"


def poller_schedule(
    queue: str,
    every_s: float,
    work_s: float,
    overlap: ScheduleOverlapPolicy,
    run_id_prefix: str = "lh201-poller-run",
) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            PollerWorkflow.run, work_s, id=run_id_prefix, task_queue=queue
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=every_s))]),
        policy=SchedulePolicy(overlap=overlap, catchup_window=timedelta(minutes=2)),
    )


def fingerprint(schedule: Schedule) -> tuple[Any, ...]:
    """The parts of a schedule that install owns. Compared to decide whether to update."""
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


async def install_schedule(client: Client, schedule_id: str, desired: Schedule) -> str:
    """Create the schedule or bring it to the desired config. Never touches the paused flag."""
    try:
        await client.create_schedule(schedule_id, desired)
        return "created"
    except ScheduleAlreadyRunningError:
        pass
    changed = False

    def updater(inp: ScheduleUpdateInput) -> ScheduleUpdate | None:
        nonlocal changed
        current = inp.description.schedule
        if fingerprint(current) == fingerprint(desired):
            return None  # returning None sends no update at all
        changed = True
        return ScheduleUpdate(schedule=dataclasses.replace(desired, state=current.state))

    await client.get_schedule_handle(schedule_id).update(updater)
    return "updated" if changed else "unchanged"


async def schedule_support_check(client: Client) -> None:
    queue = new_queue("sched")
    try:
        handle = await client.create_schedule(
            f"lh201-{uuid.uuid4().hex[:6]}",
            poller_schedule(queue, 60, 0.1, ScheduleOverlapPolicy.SKIP),
        )
        await handle.delete()
        fact("time_skipping: create_schedule", "works")
    except Exception as err:
        fact("time_skipping: create_schedule", short(err, 160))


async def install_probe(client: Client, queue: str) -> None:
    say("-- install_schedule(): create, repeat, change, and change while paused")
    schedule_id = f"lh201-poller-{uuid.uuid4().hex[:6]}"
    handle = client.get_schedule_handle(schedule_id)
    one_s = poller_schedule(queue, 1, 2.5, ScheduleOverlapPolicy.SKIP)
    two_s = poller_schedule(queue, 2, 2.5, ScheduleOverlapPolicy.SKIP)

    fact("install #1", await install_schedule(client, schedule_id, one_s))
    try:
        await client.create_schedule(schedule_id, one_s)
    except Exception as err:
        fact("create_schedule on an existing id raises", f"{type(err).__name__}: {err}")
    fact("install #2, same config", await install_schedule(client, schedule_id, one_s))
    after_2 = (await handle.describe()).info.last_updated_at
    fact("  last_updated_at after #2", after_2)

    await handle.pause(note="paused by operator")
    fact(
        "install #3, interval 1 s to 2 s, schedule paused",
        await install_schedule(client, schedule_id, two_s),
    )
    desc = await handle.describe()
    fact("  last_updated_at after #3", desc.info.last_updated_at)
    fact(
        "  paused and note after #3", f"{desc.schedule.state.paused}, {desc.schedule.state.note!r}"
    )
    fact("  interval after #3", desc.schedule.spec.intervals[0].every)
    fact("install #4, same as #3", await install_schedule(client, schedule_id, two_s))
    fact(
        "  last_updated_at unchanged by #4",
        (await handle.describe()).info.last_updated_at == desc.info.last_updated_at,
    )

    action = desc.schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    fact(
        "described action", f"workflow={action.workflow}, id={action.id}, queue={action.task_queue}"
    )
    fact(
        "described policy",
        f"{desc.schedule.policy.overlap.name}, catchup {desc.schedule.policy.catchup_window}",
    )
    await handle.delete()


async def overlap_probe(client: Client, queue: str, overlap: ScheduleOverlapPolicy) -> None:
    tag = f"{overlap.name.lower()}-{uuid.uuid4().hex[:6]}"
    schedule = poller_schedule(queue, 1, 2.5, overlap, run_id_prefix=f"lh201-run-{tag}")
    PollStats.peak = PollStats.active
    handle = await client.create_schedule(f"lh201-{tag}", schedule)
    await asyncio.sleep(12.0)
    await handle.pause()
    info = (await handle.describe()).info
    say(f"-- 12 s, 1 s interval, each poller run takes 2.5 s, overlap={overlap.name}")
    fact("actions started", info.num_actions)
    fact("ticks skipped because a run was still open", info.num_actions_skipped_overlap)
    fact("most poll_once calls running at the same time", PollStats.peak)
    if overlap is ScheduleOverlapPolicy.SKIP:
        first = info.recent_actions[0].action
        fact("workflow id of a started run", getattr(first, "workflow_id", None))
        await asyncio.sleep(3.0)  # let the open run finish so trigger() is not skipped
        before = (await handle.describe()).info.num_actions
        await handle.trigger()
        await asyncio.sleep(0.5)
        after = (await handle.describe()).info.num_actions
        fact("trigger() on a paused schedule, runs started", after - before)
    await handle.delete()


async def part_schedule(address: str | None) -> None:
    section("7. Schedules API for the poller")
    async with server(address) as (client, _):
        queue = new_queue("sched")
        async with Worker(
            client, task_queue=queue, workflows=[PollerWorkflow], activities=[poll_once]
        ):
            await install_probe(client, queue)

            await overlap_probe(client, queue, ScheduleOverlapPolicy.SKIP)
            await overlap_probe(client, queue, ScheduleOverlapPolicy.ALLOW_ALL)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
PARTS: dict[str, Callable[[str | None], Any]] = {
    "sandbox": part_sandbox,
    "pydantic": part_pydantic,
    "heartbeat": part_heartbeat,
    "can": part_can,
    "timeskip": part_timeskip,
    "idreuse": part_idreuse,
    "schedule": part_schedule,
}


async def main_async(parts: Sequence[str], address: str | None) -> None:
    for name in parts:
        await PARTS[name](address)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "worker":
        worker_main(sys.argv[2:])
        return
    # The sandbox logs a WARNING for every restricted access, even ones the probes catch, and
    # the Rust core warns about workflow tasks that the probes abandon on purpose.
    logging.basicConfig(level=logging.ERROR)
    Runtime.set_default(
        Runtime(
            telemetry=TelemetryConfig(
                logging=LoggingConfig(
                    filter=TelemetryFilter(core_level="ERROR", other_level="ERROR")
                )
            )
        )
    )
    warnings.filterwarnings("ignore", message="Module .* was imported after initial workflow load")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("parts", nargs="+", help="all or a list of: " + ", ".join(PARTS))
    parser.add_argument("--address", default=None, help="use this server instead of a dev server")
    args = parser.parse_args()
    parts = list(PARTS) if args.parts == ["all"] else args.parts
    asyncio.run(main_async(parts, args.address))


if __name__ == "__main__":
    main()
    sys.exit(0)
