"""The stuck detector inside TaskWorkflow (LH-505).

A scripted coder loops the way a stuck agent does, and the workflow feeds each coder turn to the
detector. When a rule fires, the workflow records one `stuck` event and carries on: the pause and
the question to a person are LH-506. The workflow runs on a time skipping Temporal test server
with the real llm_turn, tool_exec, append_results and record_event activities, a fake sandbox and
the memory stores, so the tests need nothing but the test server.

tests/unit/test_stuck.py has every rule on its own. These tests are about the wiring: the facts
that tool_exec works out reach the detector, a planner is never watched, the window crosses a
hand-over to a new run, and a signal that has fired is not reported again on the next turn.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from temporalio import activity
from temporalio.client import WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    BlobRef,
    Budgets,
    Outcome,
    OutcomeKind,
    Phase,
    RepoSettings,
    Role,
    Sandbox,
    TaskSpec,
)
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.events import EventActivities
from longhaul.durable.activities.llm import LlmActivities
from longhaul.durable.activities.plan import PlanActivities, StepActivities
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.durable.client import start_task
from longhaul.durable.workflow_sandbox import workflow_runner
from longhaul.durable.workflows.task import TaskWorkflow
from longhaul.providers.scripted import ScriptedProvider
from longhaul.roles import coder, planner
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.integration, pytest.mark.phase5]

BASE = "b" * 40
WAIT_S = 60.0
CFG = LoopConfig(model="test-model", max_turns=40, max_tokens_per_turn=1000, max_tokens_retry=5000)
SUMMARY = "Gave up on the lookup."
STUCK = "stuck"
KEY_ERROR = "KeyError: 'sku-7'"
FAILING_COMMAND = f'echo "{KEY_ERROR}" >&2; exit 1'

PLAN: dict[str, Any] = {
    "summary": "Fix the lookup.",
    "steps": [
        {"id": "s1", "description": "Fix it", "files": ["inv.py"], "done_when": "tests pass"},
        {"id": "s2", "description": "Test it", "files": ["t.py"], "done_when": "tests pass"},
    ],
    "acceptance_checks": ["pytest -q"],
    "risks": [],
}


# ---------- the script ----------
def tool(name: str, **arguments: Any) -> dict[str, Any]:
    return {"name": name, "input": arguments}


def turn(role: str, index: int, *calls: dict[str, Any]) -> dict[str, Any]:
    return {"match": {"role": role, "turn_index": index}, "respond": {"tool_calls": list(calls)}}


def coder_turns(*calls: dict[str, Any], then: dict[str, Any]) -> list[dict[str, Any]]:
    """One coder turn for each call, and a last turn that ends the task."""
    return [turn("coder", i, call) for i, call in enumerate([*calls, then])]


FAIL = tool("run_command", command=FAILING_COMMAND)
FINISH = tool("finish", summary=SUMMARY)


def write_script(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    path = tmp_path / f"script-{uuid.uuid4().hex[:6]}.yaml"
    path.write_text(yaml.safe_dump({"steps": steps}))
    return path


# ---------- a worker with a fake sandbox ----------
class Boxes:
    """The workspace activities: one fake sandbox for the task, with a file to read."""

    def __init__(self) -> None:
        self.boxes: dict[str, FakeSandbox] = {}

    async def get(self, sandbox_id: str) -> Sandbox | None:
        return self.boxes.get(sandbox_id)

    @activity.defn(name=acts.PREPARE_WORKSPACE.name)
    async def prepare_workspace(self, spec: TaskSpec) -> acts.PrepareWorkspaceOutput:
        box = FakeSandbox(f"sbx-{spec.task_id}")
        await box.write_file("notes.txt", b"one\ntwo\n")
        self.boxes[box.id] = box
        return acts.PrepareWorkspaceOutput(sandbox_id=box.id, base_sha=BASE, head_sha=BASE)

    @activity.defn(name=acts.CLEANUP.name)
    async def cleanup(self, inp: acts.CleanupInput) -> None:
        return None

    @activity.defn(name=acts.ENSURE_SANDBOX.name)
    async def ensure_sandbox(self, inp: acts.EnsureSandboxInput) -> str:
        return inp.sandbox_id or f"sbx-{inp.task_id}"

    @activity.defn(name=acts.BUNDLE_CHECKPOINT.name)
    async def bundle_checkpoint(self, inp: acts.BundleCheckpointInput) -> BlobRef:
        key = f"tasks/{inp.task_id}/ckpt/{inp.turn_index}.bundle"
        return BlobRef(key=key, sha256="0" * 64, size=1)

    async def destroy_all(self) -> None:
        for box in self.boxes.values():
            await box.destroy()


@dataclass
class World:
    queue: str
    recorder: MemoryRecorder

    def stuck_events(self) -> list[dict[str, Any]]:
        return [e.payload for e in self.recorder.events if e.type == STUCK]


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


def local_spec() -> TaskSpec:
    repo = "local:/work/inv"
    return TaskSpec(
        task_id=f"lh-local-inv-{uuid.uuid4().hex[:8]}",
        repo=repo,
        issue_number=None,
        title="lookup fails",
        body="`inv.lookup` raises KeyError for a sku that exists.",
        settings=RepoSettings(repo=repo, budgets=Budgets()),
    )


@asynccontextmanager
async def light(env: WorkflowEnvironment, script: Path) -> AsyncIterator[World]:
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    registry = default_registry(test_command="true", blobs=blobs)
    llm = LlmActivities(
        ScriptedProvider(script),
        blobs,
        recorder,
        registry.specs(),
        lambda role: CFG,
        role_tools={Role.PLANNER: planner.tool_specs(registry.specs())},
        planned_tools=coder.tool_specs(registry.specs()),
    )
    boxes = Boxes()
    tools = ToolActivities(registry, boxes, blobs, recorder, checkpoints=NoCheckpoints())
    queue = f"lh-test-{uuid.uuid4().hex[:8]}"
    try:
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[TaskWorkflow],
            activities=[
                *llm.activities,
                *tools.activities,
                *EventActivities(recorder).activities,
                *PlanActivities(recorder).activities,
                *StepActivities(recorder).activities,
                boxes.prepare_workspace,
                boxes.cleanup,
                boxes.ensure_sandbox,
                boxes.bundle_checkpoint,
            ],
            workflow_runner=workflow_runner(),
        ):
            yield World(queue, recorder)
    finally:
        await boxes.destroy_all()


async def result_of(handle: WorkflowHandle[Any, Outcome]) -> Outcome:
    return await asyncio.wait_for(handle.result(), WAIT_S)


# ---------- the story's test ----------
async def test_a_planted_repeated_failure_records_one_stuck_event(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """The same command fails the same way three times. One event, and the task goes on."""
    script = write_script(tmp_path, coder_turns(FAIL, FAIL, FAIL, then=FINISH))
    async with light(env, script) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        outcome = await result_of(handle)
        status = await handle.query(TaskWorkflow.status)

    (event,) = world.stuck_events()
    assert event["reason"] == "repeated_failure"
    assert event["turn_index"] == 2
    assert event["evidence"] == [f"{FAILING_COMMAND} failed 3 times with the same {KEY_ERROR}"]
    # Recording is all this story does. The pause is LH-506's, so the task finishes as asked.
    assert outcome.kind is OutcomeKind.PR_OPENED and outcome.summary == SUMMARY
    assert status.phase is Phase.DONE


async def test_a_pattern_that_goes_on_is_reported_again_only_after_it_builds_up_again(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    script = write_script(tmp_path, coder_turns(*[FAIL] * 6, then=FINISH))
    async with light(env, script) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        await result_of(handle)

    assert [e["turn_index"] for e in world.stuck_events()] == [2, 5]


async def test_two_different_failures_are_not_a_repeated_one(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    other = tool("run_command", command="echo 'ValueError: bad sku' >&2; exit 1")
    script = write_script(tmp_path, coder_turns(FAIL, other, FAIL, other, then=FINISH))
    async with light(env, script) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        outcome = await result_of(handle)

    assert world.stuck_events() == []
    assert outcome.kind is OutcomeKind.PR_OPENED


# ---------- the other rules, through the activity ----------
async def test_five_calls_that_fail_in_a_row_are_reported_with_the_last_error(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    missing = tool("read_file", path="missing.py")
    script = write_script(tmp_path, coder_turns(*[missing] * 5, then=FINISH))
    async with light(env, script) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        await result_of(handle)

    (event,) = world.stuck_events()
    assert event["reason"] == "tool_errors" and event["turn_index"] == 4
    assert event["evidence"][0] == "the last 5 tool calls failed in a row"
    assert event["evidence"][1].startswith("last error: read_file: ")


async def test_four_rewrites_of_one_file_with_no_test_run_are_edit_churn(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    rewrites = [tool("write_file", path="inv.py", content=f"x = {n}\n") for n in range(4)]
    script = write_script(tmp_path, coder_turns(*rewrites, then=FINISH))
    async with light(env, script) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        await result_of(handle)

    (event,) = world.stuck_events()
    assert event["reason"] == "edit_churn" and event["turn_index"] == 3
    assert event["evidence"] == [
        "inv.py line 1 edited 4 times in the last 10 turns with no test change"
    ]


async def test_fifteen_turns_of_reading_are_no_progress(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    read = tool("read_file", path="notes.txt")
    script = write_script(tmp_path, coder_turns(*[read] * 15, then=FINISH))
    async with light(env, script) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue)
        await result_of(handle)

    (event,) = world.stuck_events()
    assert event["reason"] == "no_progress" and event["turn_index"] == 14
    assert "15 turns" in event["evidence"][0]


# ---------- what the detector does not watch, and what it keeps ----------
async def test_a_planner_that_repeats_a_failure_is_not_stuck(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    steps = [turn("planner", i, FAIL) for i in range(4)]
    steps += [turn("planner", 4, tool("submit_plan", **PLAN))]
    steps += [turn("coder", 0, tool("submit_for_review", summary=SUMMARY))]
    async with light(env, write_script(tmp_path, steps)) as world:
        handle = await start_task(env.client, local_spec(), task_queue=world.queue, plan=True)
        outcome = await result_of(handle)

    assert world.stuck_events() == []
    assert outcome.kind is OutcomeKind.PR_OPENED


async def test_the_window_crosses_a_hand_over_to_a_new_run(
    env: WorkflowEnvironment, tmp_path: Path
) -> None:
    """Two failures, a hand-over after the second turn, and the third failure in the new run."""
    script = write_script(tmp_path, coder_turns(FAIL, FAIL, FAIL, then=FINISH))
    async with light(env, script) as world:
        handle = await start_task(
            env.client,
            local_spec(),
            task_queue=world.queue,
            tuning=acts.Tuning(continue_as_new_every_turns=2),
        )
        await result_of(handle)

    assert [e.type for e in world.recorder.events].count("continue_as_new") >= 1
    (event,) = world.stuck_events()
    assert event["reason"] == "repeated_failure" and event["turn_index"] == 2
