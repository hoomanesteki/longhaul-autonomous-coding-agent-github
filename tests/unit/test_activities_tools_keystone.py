"""tool_exec with the Keystone backend (LH-909).

The activity keeps everything around a call: the start from `pre_sha`, the stored text, the
`tool_calls` row and the checkpoint. Only the step that runs the tool changes. These tests run
the real activity against a fake Keystone and check what the backend adds: a denied call is a
failed result that the row records, a call held for approval carries the question that pauses
the task, and a sandbox that vanished during a remote call is still reported as gone.
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator
from typing import Any

import pytest
from longhaul.tools.keystone_backend import KeystoneBackend
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tests.fakes.fake_keystone import FakeKeystone, running_keystone

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import Sandbox
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.tools import NoCheckpoints, ToolActivities
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase9]

TASK = "lh-acme-calc-42"


class Lookup:
    """Finds the sandbox, and can start to lose it after a number of lookups."""

    def __init__(self, sandbox: Sandbox, *, lost_after: int | None = None) -> None:
        self.sandbox = sandbox
        self.lost_after = lost_after
        self.lookups = 0

    async def get(self, sandbox_id: str) -> Sandbox | None:
        self.lookups += 1
        if self.lost_after is not None and self.lookups > self.lost_after:
            return None
        return self.sandbox if self.sandbox.id == sandbox_id else None


@pytest.fixture
async def keystone() -> AsyncIterator[FakeKeystone]:
    async with running_keystone() as fake:
        yield fake


@pytest.fixture
async def sandbox() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox("sbx-1")
    yield box
    await box.destroy()


@dataclasses.dataclass
class Rig:
    activities: ToolActivities
    recorder: MemoryRecorder
    backend: KeystoneBackend

    async def exec(self, name: str, **arguments: Any) -> Any:
        inp = acts.ToolExecInput(
            task_id=TASK,
            turn_index=2,
            call=acts.ToolCallSummary(id="toolu_1", name=name, input=arguments),
            pre_sha=None,
            sandbox_id="sbx-1",
        )
        return await ActivityEnvironment().run(self.activities.tool_exec, inp)


@pytest.fixture
async def rig(keystone: FakeKeystone, sandbox: FakeSandbox) -> AsyncIterator[Rig]:
    blobs = MemoryBlobStore()
    recorder = MemoryRecorder()
    backend = KeystoneBackend(keystone.url, token=lambda: "t")
    activities = ToolActivities(
        default_registry(blobs=blobs),
        Lookup(sandbox),
        blobs,
        recorder,
        checkpoints=NoCheckpoints(),
        backend=backend,
    )
    yield Rig(activities, recorder, backend)
    await backend.aclose()


async def test_an_allowed_call_is_stored_and_recorded_like_a_direct_one(rig: Rig) -> None:
    result = await rig.exec("run_command", command="echo hi")

    assert result.ok
    assert result.approval_question is None
    assert result.output == "exit code: 0\nran echo hi"
    assert result.output_ref is not None
    row = rig.recorder.tool_calls["toolu_1"]
    assert row.ok and row.name == "run_command"


async def test_a_denied_call_is_a_failed_result_and_a_failed_row(rig: Rig) -> None:
    result = await rig.exec("run_command", command="curl http://example.com")

    assert not result.ok
    assert result.approval_question is None
    assert "KS_DENIED" in result.output
    row = rig.recorder.tool_calls["toolu_1"]
    assert not row.ok


async def test_a_call_held_for_approval_carries_the_question(rig: Rig) -> None:
    result = await rig.exec("run_command", command="deploy now")

    assert not result.ok
    assert result.approval_question is not None
    assert "approve-deploy" in result.approval_question
    row = rig.recorder.tool_calls["toolu_1"]
    assert not row.ok


async def test_a_direct_result_has_no_question(sandbox: FakeSandbox) -> None:
    blobs = MemoryBlobStore()
    activities = ToolActivities(
        default_registry(blobs=blobs),
        Lookup(sandbox),
        blobs,
        MemoryRecorder(),
        checkpoints=NoCheckpoints(),
    )
    inp = acts.ToolExecInput(
        task_id=TASK,
        turn_index=0,
        call=acts.ToolCallSummary(id="c", name="run_command", input={"command": "echo hi"}),
        pre_sha=None,
        sandbox_id="sbx-1",
    )

    result = await ActivityEnvironment().run(activities.tool_exec, inp)

    assert result.ok and result.approval_question is None


async def test_a_sandbox_lost_during_a_remote_call_is_reported_as_gone(
    keystone: FakeKeystone, sandbox: FakeSandbox
) -> None:
    blobs = MemoryBlobStore()
    backend = KeystoneBackend(keystone.url, token=lambda: "t")
    # The first lookup finds the box. By the time the call has failed, it is gone.
    activities = ToolActivities(
        default_registry(blobs=blobs),
        Lookup(sandbox, lost_after=1),
        blobs,
        MemoryRecorder(),
        checkpoints=NoCheckpoints(),
        backend=backend,
    )
    inp = acts.ToolExecInput(
        task_id=TASK,
        turn_index=0,
        call=acts.ToolCallSummary(id="c", name="run_command", input={"command": "curl x"}),
        pre_sha=None,
        sandbox_id="sbx-1",
    )
    try:
        with pytest.raises(ApplicationError) as raised:
            await ActivityEnvironment().run(activities.tool_exec, inp)
    finally:
        await backend.aclose()

    assert raised.value.type == acts.SANDBOX_GONE
    assert raised.value.non_retryable


async def test_a_remote_result_that_was_ok_does_not_look_for_the_sandbox(
    keystone: FakeKeystone, sandbox: FakeSandbox
) -> None:
    blobs = MemoryBlobStore()
    backend = KeystoneBackend(keystone.url, token=lambda: "t")
    lookup = Lookup(sandbox)
    activities = ToolActivities(
        default_registry(blobs=blobs),
        lookup,
        blobs,
        MemoryRecorder(),
        checkpoints=NoCheckpoints(),
        backend=backend,
    )
    inp = acts.ToolExecInput(
        task_id=TASK,
        turn_index=0,
        call=acts.ToolCallSummary(id="c", name="run_command", input={"command": "echo hi"}),
        pre_sha=None,
        sandbox_id="sbx-1",
    )
    try:
        await ActivityEnvironment().run(activities.tool_exec, inp)
    finally:
        await backend.aclose()

    assert lookup.lookups == 1
