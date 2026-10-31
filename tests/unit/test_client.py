"""start_task and connect: what they hand to the Temporal client.

A fake client records the call. The integration tests start real workflows against a test
server; this file pins the arguments, which are the part a typo can break quietly.
"""

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter

from longhaul.contracts import CarryState, Phase, RepoSettings, TaskSpec
from longhaul.durable import client as durable_client
from longhaul.durable.acts import TUNING_MEMO_KEY, Tuning
from longhaul.durable.client import DEFAULT_TASK_QUEUE, connect, start_task
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def start_workflow(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append((args, kwargs))
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


async def test_connect_uses_the_pydantic_data_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(address: str, **kwargs: Any) -> str:
        seen["address"] = address
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr(durable_client.Client, "connect", fake_connect)

    assert await connect("temporal.internal:7233", namespace="prod") == "client"
    assert seen == {
        "address": "temporal.internal:7233",
        "namespace": "prod",
        "data_converter": pydantic_data_converter,
    }


async def test_connect_defaults_to_the_default_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(address: str, **kwargs: Any) -> str:
        seen.update(kwargs)
        return "client"

    monkeypatch.setattr(durable_client.Client, "connect", fake_connect)

    await connect("localhost:7233")
    assert seen["namespace"] == "default"
