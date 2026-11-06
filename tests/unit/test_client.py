"""start_task and connect: what they hand to the Temporal client.

A fake client records the call. The integration tests start real workflows against a test
server; this file pins the arguments, which are the part a typo can break quietly.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.opentelemetry import TracingInterceptor

from longhaul.context.modes import ContextMode
from longhaul.contracts import BlobRef, CarryState, Phase, RepoSettings, TaskSpec, TaskStatus
from longhaul.durable import client as durable_client
from longhaul.durable.acts import (
    CONTEXT_MODE_MEMO_KEY,
    PLAN_MEMO_KEY,
    SCRIPT_MEMO_KEY,
    TUNING_MEMO_KEY,
    Tuning,
)
from longhaul.durable.client import (
    DEFAULT_TASK_QUEUE,
    cancel_task,
    connect,
    get_task,
    read_status,
    start_task,
)
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.settings import EnvSettings
from longhaul.telemetry import otel
from longhaul.telemetry.logging import LogContextInterceptor

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.current_spans: list[trace.Span] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
        self.current_spans.append(trace.get_current_span())
        return "handle"


@pytest.fixture
def spec() -> TaskSpec:
    return TaskSpec(
        task_id="lh-acme-calc-42",
        repo="acme/calc",
        issue_number=42,
        title="add() subtracts",
        body="add(2, 3) returns -1",
        settings=RepoSettings(repo="acme/calc"),
    )


async def test_start_task_uses_the_task_id_as_the_workflow_id(spec: TaskSpec) -> None:
    fake = FakeClient()
    handle = await start_task(cast(Client, fake), spec)

    assert handle == "handle"
    ((args, kwargs),) = fake.calls
    assert args[0] == TaskWorkflow.run
    assert kwargs["id"] == "lh-acme-calc-42"
    assert kwargs["task_queue"] == DEFAULT_TASK_QUEUE


async def test_start_task_rejects_a_duplicate_while_open_and_after_close(spec: TaskSpec) -> None:
    # ADR-0004 section 6. With the SDK defaults a finished task would start again, and the
    # poller would rerun it as long as the label stays on the issue.
    fake = FakeClient()
    await start_task(cast(Client, fake), spec)

    kwargs = fake.calls[0][1]
    assert kwargs["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert kwargs["id_conflict_policy"] is WorkflowIDConflictPolicy.FAIL


async def test_start_task_builds_the_first_carry_state(spec: TaskSpec) -> None:
    fake = FakeClient()
    before = datetime.now(UTC)
    await start_task(cast(Client, fake), spec)

    state = fake.calls[0][0][1]
    assert isinstance(state, CarryState)
    assert state.spec == spec
    assert state.phase is Phase.QUEUED
    assert (state.turn_index, state.cost_usd, state.hints) == (0, 0.0, [])
    assert state.sandbox_id is None and state.head_sha is None
    # Workflow code subtracts started_at from workflow.now(), which carries a timezone.
    assert state.started_at.tzinfo is not None
    assert before <= state.started_at <= datetime.now(UTC)


async def test_start_task_starts_the_workflow_outside_the_callers_span(spec: TaskSpec) -> None:
    # The poller calls start_task from an activity. A task started under that span would share
    # its trace with every other task of the poll.
    fake = FakeClient()
    provider = TracerProvider()
    with provider.get_tracer("test").start_as_current_span("RunActivity:poll_once") as caller:
        await start_task(cast(Client, fake), spec)
        assert trace.get_current_span() is caller

    (seen,) = fake.current_spans
    assert not seen.get_span_context().is_valid


async def test_start_task_sends_no_memo_by_default(spec: TaskSpec) -> None:
    fake = FakeClient()
    await start_task(cast(Client, fake), spec)
    assert fake.calls[0][1]["memo"] is None


async def test_start_task_passes_tuning_in_the_memo(spec: TaskSpec) -> None:
    fake = FakeClient()
    tuning = Tuning(heartbeat_timeout_s=2)
    await start_task(cast(Client, fake), spec, tuning=tuning)
    assert fake.calls[0][1]["memo"] == {TUNING_MEMO_KEY: tuning}


async def test_start_task_takes_a_task_queue(spec: TaskSpec) -> None:
    fake = FakeClient()
    await start_task(cast(Client, fake), spec, task_queue="other")
    assert fake.calls[0][1]["task_queue"] == "other"


def test_the_default_task_queue_is_the_one_in_the_settings() -> None:
    assert EnvSettings.model_fields["task_queue"].default == DEFAULT_TASK_QUEUE


async def test_connect_uses_the_guarded_data_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(address: str, **kwargs: Any) -> str:
        seen["address"] = address
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr(durable_client.Client, "connect", fake_connect)

    assert await connect("temporal.internal:7233", namespace="prod") == "client"
    interceptors = seen.pop("interceptors")
    assert seen == {
        "address": "temporal.internal:7233",
        "namespace": "prod",
        "data_converter": guarded_data_converter,
    }
    assert [type(i) for i in interceptors] == [LogContextInterceptor]  # tracing is off here


async def test_connect_defaults_to_the_default_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(address: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr(durable_client.Client, "connect", fake_connect)

    await connect("localhost:7233")
    assert seen["namespace"] == "default"


async def test_connect_registers_the_tracing_interceptor_when_tracing_is_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker takes its interceptors from its client, so this one registration also covers
    # the worker's activities.
    seen: dict[str, Any] = {}

    async def fake_connect(address: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr(durable_client.Client, "connect", fake_connect)

    with otel.configured(TracerProvider()):
        await connect("localhost:7233")

    # Tracing goes first, so its RunActivity span is open when the logging interceptor runs.
    tracing, logging_context = seen["interceptors"]
    assert isinstance(tracing, TracingInterceptor)
    assert isinstance(logging_context, LogContextInterceptor)


async def test_connect_registers_the_logging_interceptor_without_tracing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without it a worker activity logs no task_id, turn_index or activity. It sits on the client
    # because the worker takes its interceptors from there.
    seen: dict[str, Any] = {}

    async def fake_connect(address: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr(durable_client.Client, "connect", fake_connect)

    await connect("localhost:7233")

    (interceptor,) = seen["interceptors"]
    assert isinstance(interceptor, LogContextInterceptor)


# ---------- LH-208: a script in the memo, and the handle calls of the CLI ----------
SCRIPT = BlobRef(key="tasks/lh-acme-calc-42/script.yaml", sha256="1" * 64, size=12)


async def test_start_task_passes_the_script_in_the_memo(spec: TaskSpec) -> None:
    fake = FakeClient()
    await start_task(cast(Client, fake), spec, script=SCRIPT)
    assert fake.calls[0][1]["memo"] == {SCRIPT_MEMO_KEY: SCRIPT}


async def test_start_task_passes_the_script_and_the_tuning_together(spec: TaskSpec) -> None:
    fake = FakeClient()
    tuning = Tuning(heartbeat_timeout_s=2)
    await start_task(cast(Client, fake), spec, tuning=tuning, script=SCRIPT)
    assert fake.calls[0][1]["memo"] == {TUNING_MEMO_KEY: tuning, SCRIPT_MEMO_KEY: SCRIPT}


# ---------- LH-406: the context mode of a run ----------
async def test_start_task_passes_the_context_mode_in_the_memo(spec: TaskSpec) -> None:
    fake = FakeClient()
    await start_task(cast(Client, fake), spec, context_mode=ContextMode.NONE)
    assert fake.calls[0][1]["memo"] == {CONTEXT_MODE_MEMO_KEY: ContextMode.NONE}


async def test_start_task_sends_no_mode_unless_the_run_names_one(spec: TaskSpec) -> None:
    # Without a mode the config of the worker decides, so the memo has nothing to say.
    fake = FakeClient()
    await start_task(cast(Client, fake), spec, script=SCRIPT)
    assert CONTEXT_MODE_MEMO_KEY not in fake.calls[0][1]["memo"]


async def test_start_task_passes_the_mode_with_the_script_and_the_tuning(spec: TaskSpec) -> None:
    fake = FakeClient()
    tuning = Tuning(heartbeat_timeout_s=2)
    await start_task(
        cast(Client, fake),
        spec,
        tuning=tuning,
        script=SCRIPT,
        context_mode=ContextMode.NO_COMPACTION,
    )
    assert fake.calls[0][1]["memo"] == {
        TUNING_MEMO_KEY: tuning,
        SCRIPT_MEMO_KEY: SCRIPT,
        CONTEXT_MODE_MEMO_KEY: ContextMode.NO_COMPACTION,
    }


class FakeHandle:
    def __init__(self) -> None:
        self.queries: list[tuple[Any, dict[str, Any]]] = []
        self.signals: list[tuple[Any, Any]] = []

    async def query(self, query: Any, **kwargs: Any) -> TaskStatus:
        self.queries.append((query, kwargs))
        return TaskStatus(
            task_id="lh-acme-calc-42",
            phase=Phase.CODING,
            turn_index=3,
            cost_usd=0.0,
            last_checkpoint=None,
            plan_version=None,
        )

    async def signal(self, signal: Any, *, args: Any) -> None:
        self.signals.append((signal, args))


class FakeLookup:
    def __init__(self) -> None:
        self.handle = FakeHandle()
        self.asked: list[tuple[Any, str]] = []

    def get_workflow_handle_for(self, workflow: Any, workflow_id: str) -> FakeHandle:
        self.asked.append((workflow, workflow_id))
        return self.handle


def test_get_task_finds_the_workflow_by_its_task_id() -> None:
    fake = FakeLookup()
    handle = get_task(cast(Client, fake), "lh-acme-calc-42")
    assert handle is fake.handle
    assert fake.asked == [(TaskWorkflow.run, "lh-acme-calc-42")]


async def test_read_status_queries_the_workflow_with_a_deadline() -> None:
    fake = FakeLookup()
    status = await read_status(cast(Any, fake.handle), timeout_s=3)

    assert status.turn_index == 3
    ((query, kwargs),) = fake.handle.queries
    assert query == TaskWorkflow.status
    assert kwargs == {"rpc_timeout": timedelta(seconds=3)}


async def test_cancel_task_signals_who_asked_and_why() -> None:
    fake = FakeLookup()
    await cancel_task(cast(Client, fake), "lh-acme-calc-42", by="cli", reason="wrong repo")

    assert fake.asked == [(TaskWorkflow.run, "lh-acme-calc-42")]
    assert fake.handle.signals == [(TaskWorkflow.cancel, ["cli", "wrong repo"])]


async def test_connect_names_the_address_when_the_server_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def refuse(address: str, **kwargs: Any) -> str:
        raise RuntimeError("Failed client connect: connection refused")

    monkeypatch.setattr(durable_client.Client, "connect", refuse)

    with pytest.raises(ConnectionError, match="cannot reach Temporal at localhost:1"):
        await connect("localhost:1")


# ---------- LH-501: planning is switched on per task ----------
async def test_start_task_does_not_plan_unless_asked(spec: TaskSpec) -> None:
    fake = FakeClient()
    await start_task(cast(Client, fake), spec, plan=False)
    assert fake.calls[0][1]["memo"] is None


async def test_start_task_passes_the_plan_switch_in_the_memo(spec: TaskSpec) -> None:
    fake = FakeClient()
    await start_task(cast(Client, fake), spec, plan=True)
    assert fake.calls[0][1]["memo"] == {PLAN_MEMO_KEY: True}


async def test_start_task_passes_the_plan_switch_with_the_script_and_the_tuning(
    spec: TaskSpec,
) -> None:
    fake = FakeClient()
    tuning = Tuning(heartbeat_timeout_s=2)
    await start_task(cast(Client, fake), spec, tuning=tuning, script=SCRIPT, plan=True)
    assert fake.calls[0][1]["memo"] == {
        TUNING_MEMO_KEY: tuning,
        SCRIPT_MEMO_KEY: SCRIPT,
        PLAN_MEMO_KEY: True,
    }
