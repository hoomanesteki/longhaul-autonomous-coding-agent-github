"""LH-704: the metrics of a scripted run, read back from the /metrics endpoint.

The run is TaskWorkflow with the real activities of a worker: prepare_workspace, ensure_sandbox,
llm_turn, tool_exec, append_results, cleanup and record_event. Docker runs the sandboxes and a
real checkpoint store keeps the bare repo, as in tests/integration/test_sandbox_recovery.py, and
a time skipping Temporal test server runs the workflow. The provider is scripted. The ledger,
the blobs and the event log are in memory, because the metrics do not depend on Postgres.

The script makes four things happen that an operator wants to see counted:

* the model is overloaded once on its first turn, so llm_turn is retried and an error is counted;
* the sandbox is removed from outside before the third turn, so a tool call finds it gone and
  ensure_sandbox builds a new one at the last checkpoint;
* the tools write, read and list files;
* the run ends with `finish`.

Compactions and stuck pauses come from stories that are not built yet (LH-404, LH-505). The run
makes none, and the test checks that both metrics are there and at zero. The tests of the
record_event activity (tests/unit/test_activities_events.py) show how an event of that kind is
counted.

Every task, container, volume and task queue has a name with a random suffix, and the fixture
removes what the test made. The setup command is empty, so the sandbox needs no network.
"""

import asyncio
import shutil
import urllib.request
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docker
import pytest
import yaml
from prometheus_client.parser import text_string_to_metric_families
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.contracts import (
    Budgets,
    LLMResponse,
    Outcome,
    OutcomeKind,
    RepoSettings,
    SandboxLimits,
    TaskSpec,
)
from longhaul.cost.ledger import MemoryLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.tools import ToolActivities
from longhaul.durable.activities.workspace import WorkspaceActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.scripted import ScriptedProvider
from longhaul.sandbox.docker_manager import TASK_LABEL, DockerSandboxManager
from longhaul.telemetry import metrics
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase7]

ROOT = Path(__file__).resolve().parents[2]
CALC = ROOT / "tests" / "fixtures" / "repos" / "calc"
PRICING = ROOT / "config" / "pricing.yaml"
IMAGE = "longhaul-sandbox:py312"
SONNET = "claude-sonnet-5-5"  # a model with a price, so the run has a cost
CFG = LoopConfig(model=SONNET, max_turns=10, max_tokens_per_turn=1000, max_tokens_retry=5000)
# The heartbeat timeouts of section 3.7 are 15 to 60 s. A run in a test shortens them, and the
# activities must still heartbeat inside the shorter one.
SHORT_HEARTBEAT = acts.Tuning(heartbeat_timeout_s=3)
RUN_WAIT_S = 300.0

# The turn before which the sandbox is removed, counted in assistant messages.
KILL_AT = 2

# Name and kind of every metric of the story, as /metrics writes them.
EXPOSED = {
    "longhaul_tasks_total": "counter",
    "longhaul_active_tasks": "gauge",
    "longhaul_turns_total": "counter",
    "longhaul_llm_cost_usd_total": "counter",
    "longhaul_llm_errors_total": "counter",
    "longhaul_tool_duration_seconds": "histogram",
    "longhaul_compactions_total": "counter",
    "longhaul_stuck_total": "counter",
    "longhaul_sandbox_recreated_total": "counter",
    "longhaul_activity_retries_total": "counter",
    "longhaul_cache_ratio": "histogram",
}


# ---------- the script ----------
def write_script(directory: Path) -> Path:
    steps: list[dict[str, Any]] = [
        # Turn 0 is overloaded once. llm_turn raises a retryable error and Temporal runs it again.
        {"match": {"turn_index": 0}, "respond": {"raise": "overloaded", "times": 1}},
        {
            "match": {"turn_index": 0},
            "respond": {
                "text": "Writing the notes.",
                "tool_calls": [
                    {"name": "write_file", "input": {"path": "notes.txt", "content": "first\n"}}
                ],
            },
        },
        {
            "match": {"turn_index": 1},
            "respond": {
                "tool_calls": [
                    {"name": "read_file", "input": {"path": "notes.txt"}},
                    {"name": "list_dir", "input": {"path": "."}},
                ]
            },
        },
        {
            "match": {"turn_index": 2},
            "respond": {
                "tool_calls": [
                    {"name": "write_file", "input": {"path": "notes.txt", "content": "second\n"}}
                ]
            },
        },
        {
            "match": {"turn_index": 3},
            "respond": {"tool_calls": [{"name": "finish", "input": {"summary": "Wrote notes."}}]},
        },
    ]
    path = directory / "metrics.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    return path


class KillsTheSandbox(ScriptedProvider):
    """A scripted provider that removes the task's containers just before it answers a turn.

    The container goes while the workflow is between two tool calls, as it does when someone
    runs `docker rm -f` on the machine.
    """

    def __init__(self, script: Path, *, at: int, task_id: str) -> None:
        super().__init__(script)
        self.at, self.task_id = at, task_id
        self.killed = False

    async def complete(self, **kwargs: Any) -> LLMResponse:
        turns = sum(1 for m in kwargs["messages"] if m.role == "assistant")
        if turns == self.at and not self.killed:
            self.killed = True
            await asyncio.to_thread(remove_containers, self.task_id)
        return await super().complete(**kwargs)


def remove_containers(task_id: str) -> None:
    for container in docker.from_env().containers.list(
        all=True, filters={"label": f"{TASK_LABEL}={task_id}"}
    ):
        container.remove(force=True)


# ---------- a worker with the real activities ----------
@dataclass
class Stack:
    task_id: str
    source: Path
    manager: DockerSandboxManager
    store: GitCheckpointStore
    blobs: MemoryBlobStore
    recorder: MemoryRecorder
    ledger: MemoryLedger
    queue: str = field(default_factory=lambda: f"lh-metrics-{uuid.uuid4().hex[:8]}")

    def spec(self) -> TaskSpec:
        repo = f"local:{self.source}"
        return TaskSpec(
            task_id=self.task_id,
            repo=repo,
            issue_number=42,
            title="Write the notes file",
            body="Please write notes.txt.",
            # An empty setup command means no setup, so no network is needed.
            settings=RepoSettings(repo=repo, setup_command="", budgets=Budgets(task_turns=10)),
        )


@pytest.fixture
async def stack(tmp_path: Path) -> AsyncIterator[Stack]:
    # The id ends in the issue number, so the scratch branch is longhaul/42.
    task_id = f"lh-met{uuid.uuid4().hex[:8]}-42"
    blobs = MemoryBlobStore()
    store = GitCheckpointStore(blobs, root=tmp_path / "ckpt")
    manager = DockerSandboxManager()
    built = Stack(
        task_id=task_id,
        source=shutil.copytree(CALC, tmp_path / "calc"),
        manager=manager,
        store=store,
        blobs=blobs,
        recorder=MemoryRecorder(),
        ledger=MemoryLedger(Pricing.load(PRICING)),
    )
    try:
        yield built
    finally:
        await manager.cleanup_task(task_id)
        await store.cleanup_task(task_id)


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


@pytest.fixture
def endpoint(counters: metrics.Metrics) -> Iterator[metrics.MetricsServer]:
    """The /metrics endpoint of a worker, on a free port, over the metrics of this test."""
    server = metrics.serve(0, "127.0.0.1", counters.registry)
    try:
        yield server
    finally:
        server.stop()


@asynccontextmanager
async def running(
    env: WorkflowEnvironment, stack: Stack, provider: ScriptedProvider
) -> AsyncIterator[str]:
    """A worker as `serve` builds one: the real activities and the retry counter."""
    registry = default_registry()
    llm = LlmActivities(
        provider,
        stack.blobs,
        stack.recorder,
        registry.specs(),
        lambda role: CFG,
        ledger=stack.ledger,
    )
    tools = ToolActivities(
        registry, stack.manager, stack.blobs, stack.recorder, checkpoints=stack.store
    )
    workspace = WorkspaceActivities(
        stack.manager, stack.store, stack.recorder, image=IMAGE, limits=SandboxLimits()
    )
    events = EventActivities(stack.recorder)
    async with Worker(
        env.client,
        task_queue=stack.queue,
        workflows=[TaskWorkflow],
        activities=[
            *llm.activities,
            *tools.activities,
            *workspace.activities,
            *events.activities,
        ],
        workflow_runner=workflow_runner(),
        interceptors=[metrics.RetryCounter()],
    ):
        yield stack.queue


async def result_of(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    return await asyncio.wait_for(handle.result(), RUN_WAIT_S)


# ---------- reading /metrics ----------
Samples = dict[tuple[str, frozenset[tuple[str, str]]], float]


def scrape(server: metrics.MetricsServer) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/metrics", timeout=10) as reply:
        return str(reply.read().decode())


def samples_of(text: str) -> Samples:
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


def reading(samples: Samples, name: str, **labels: str) -> float:
    """One sample, or a failure that lists what the name does have, so a typo shows itself."""
    key = (name, frozenset(labels.items()))
    if key not in samples:
        have = sorted(str(sorted(k[1])) for k in samples if k[0] == name)
        raise AssertionError(f"no sample {name} {labels}; the series of that name are {have}")
    return samples[key]


# ---------- LH-704 acceptance criteria ----------
async def test_metrics_exposed_after_run(
    env: WorkflowEnvironment,
    stack: Stack,
    counters: metrics.Metrics,
    endpoint: metrics.MetricsServer,
    tmp_path: Path,
) -> None:
    """After a scripted run, /metrics exposes every metric of the story, and the ones the run
    makes are not zero."""
    provider = KillsTheSandbox(write_script(tmp_path), at=KILL_AT, task_id=stack.task_id)
    async with running(env, stack, provider) as queue:
        handle = await start_task(
            env.client, stack.spec(), task_queue=queue, tuning=SHORT_HEARTBEAT
        )
        outcome = await result_of(handle)

    assert provider.killed
    assert outcome == Outcome(kind=OutcomeKind.PR_OPENED, summary="Wrote notes.")

    text = scrape(endpoint)
    for name, kind in EXPOSED.items():
        assert f"# TYPE {name} {kind}" in text, f"/metrics does not expose {name}"
    samples = samples_of(text)

    # One task, which ended with a pull request and is no longer active. Its repo is a local
    # folder, which has one label however many folders are run.
    assert reading(samples, "longhaul_tasks_total", outcome="pr_opened", repo="local") == 1
    assert reading(samples, "longhaul_active_tasks") == 0

    # Four model calls came back with a reply: one per turn. The overloaded try is an error and
    # not a turn, and llm_turn ran again for it.
    assert reading(samples, "longhaul_turns_total", role="coder") == 4
    assert reading(samples, "longhaul_llm_errors_total", kind="overloaded") == 1
    assert reading(samples, "longhaul_activity_retries_total", activity="llm_turn") == 1
    assert reading(samples, "longhaul_cache_ratio_count") == 4

    # The cost is what the ledger priced, summed over the same four calls.
    spent = sum(float(entry.usd) for entry in stack.ledger.entries)
    assert spent > 0
    assert len(stack.ledger.entries) == 4
    cost = reading(samples, "longhaul_llm_cost_usd_total", model=SONNET, repo="local")
    assert cost == pytest.approx(spent)

    # Four tool runs took time. The write that found the sandbox gone ran for the second time
    # in the new sandbox, and the first try never reached the tool.
    assert reading(samples, "longhaul_tool_duration_seconds_count", tool="write_file") == 2
    assert reading(samples, "longhaul_tool_duration_seconds_count", tool="read_file") == 1
    assert reading(samples, "longhaul_tool_duration_seconds_count", tool="list_dir") == 1
    assert reading(samples, "longhaul_tool_duration_seconds_sum", tool="write_file") > 0

    assert reading(samples, "longhaul_sandbox_recreated_total") == 1

    # Nothing in this run compacts its transcript or gets stuck. Both are still reported.
    assert reading(samples, "longhaul_compactions_total") == 0
    assert reading(samples, "longhaul_stuck_total", reason="no_progress") == 0


async def test_a_task_is_active_while_it_runs(
    env: WorkflowEnvironment,
    stack: Stack,
    counters: metrics.Metrics,
    endpoint: metrics.MetricsServer,
    tmp_path: Path,
) -> None:
    """The gauge goes up in prepare_workspace and comes down when the run ends."""
    seen: list[float] = []

    class Watching(ScriptedProvider):
        async def complete(self, **kwargs: Any) -> LLMResponse:
            seen.append(reading(samples_of(scrape(endpoint)), "longhaul_active_tasks"))
            return await super().complete(**kwargs)

    script = tmp_path / "short.yaml"
    script.write_text(
        yaml.safe_dump(
            {
                "steps": [
                    {
                        "respond": {
                            "tool_calls": [{"name": "finish", "input": {"summary": "Nothing."}}]
                        }
                    }
                ]
            }
        )
    )
    async with running(env, stack, Watching(script)) as queue:
        handle = await start_task(env.client, stack.spec(), task_queue=queue)
        await result_of(handle)

    assert seen == [1.0]
    assert reading(samples_of(scrape(endpoint)), "longhaul_active_tasks") == 0
