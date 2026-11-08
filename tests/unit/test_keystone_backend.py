"""The Keystone tool backend (LH-909).

With `tools.backend: keystone` the worker calls each sandbox tool through Keystone's MCP
endpoint, and Keystone decides it. These tests run the backend against a fake Keystone, an MCP
endpoint on this machine that allows, denies or holds a call the way the real one answers
(tests/fakes/fake_keystone.py). The mapping under test: an allow is the tool's own result, a
deny is a failed result with Keystone's reason, a call held for approval is a failed result that
also asks for a pause, and the task id keeps every call of one task in one Keystone session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from longhaul.tools.backend import DirectBackend, Ran
from longhaul.tools.keystone_backend import KeystoneBackend
from longhaul.tools.mcp_server import CONTEXT_KEY
from tests.fakes.fake_keystone import AWAIT_TOOL, FakeKeystone, running_keystone

from longhaul.contracts import ToolCall, ToolContext
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase9]


@pytest.fixture
async def keystone() -> AsyncIterator[FakeKeystone]:
    async with running_keystone() as fake:
        yield fake


@pytest.fixture
async def sandbox() -> AsyncIterator[FakeSandbox]:
    box = FakeSandbox("sbx-1")
    yield box
    await box.destroy()


@pytest.fixture
async def backend(keystone: FakeKeystone) -> AsyncIterator[KeystoneBackend]:
    found = KeystoneBackend(keystone.url, token=lambda: "secret-token")
    yield found
    await found.aclose()


async def run(
    backend: KeystoneBackend,
    sandbox: FakeSandbox,
    name: str,
    *,
    task: str = "lh-task-1",
    call_id: str = "toolu_1",
    **arguments: object,
) -> Ran:
    call = ToolCall(id=call_id, name=name, input=dict(arguments))
    ctx = ToolContext(task_id=task, turn_index=3, blob_prefix=f"tasks/{task}")
    return await backend.run(default_registry(), sandbox, call, ctx, sandbox_id=sandbox.id)


# ---------- the three decisions ----------
async def test_an_allowed_call_returns_the_tools_own_result(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    ran = await run(backend, sandbox, "run_command", command="echo hi")

    assert ran.approval_question is None
    assert ran.result.ok
    assert ran.result.call_id == "toolu_1"
    assert ran.result.output == "exit code: 0\nran echo hi"
    # The first line of a command's output is where its exit code is read from.
    assert ran.result.exit_code == 0
    (seen,) = keystone.calls
    assert seen.tool == "longhaul_sandbox__run_command"


async def test_a_denied_call_is_a_failed_result_with_the_reason(
    backend: KeystoneBackend, sandbox: FakeSandbox
) -> None:
    ran = await run(backend, sandbox, "run_command", command="curl http://example.com")

    assert not ran.result.ok
    assert ran.approval_question is None
    assert "KS_DENIED" in ran.result.output
    assert "policy block-curl forbids this command" in ran.result.output


async def test_a_call_held_for_approval_asks_for_a_pause(
    backend: KeystoneBackend, sandbox: FakeSandbox
) -> None:
    ran = await run(backend, sandbox, "run_command", command="deploy now")

    assert not ran.result.ok
    assert ran.approval_question is not None
    # The reason is the question a person reads, and the request they have to decide.
    assert "policy approve-deploy needs a person to approve this command" in ran.approval_question
    assert "apr-1" in ran.approval_question
    # The model is told the call did not run and that the task waits for a person.
    assert "not run" in ran.result.output
    assert "apr-1" in ran.result.output


async def test_a_rejected_approval_is_a_failed_result_and_not_a_pause(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    await run(backend, sandbox, "run_command", command="deploy now")
    keystone.decide("apr-1", "rejected")

    ran = await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_2")

    assert not ran.result.ok
    assert ran.approval_question is None
    assert "KS_APPROVAL_REJECTED" in ran.result.output


async def test_the_same_call_after_an_approval_runs_through_the_await_tool(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    await run(backend, sandbox, "run_command", command="deploy now")
    keystone.decide("apr-1", "approved")

    ran = await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_2")

    assert ran.result.ok
    assert ran.result.call_id == "toolu_2"
    assert "(approved)" in ran.result.output
    assert [seen.tool for seen in keystone.calls] == [
        "longhaul_sandbox__run_command",
        AWAIT_TOOL,
    ]
    # The request is used once. A third call is a new call.
    again = await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_3")
    assert again.approval_question is not None
    assert keystone.calls[-1].tool == "longhaul_sandbox__run_command"


async def test_a_call_that_is_still_waiting_pauses_again(
    backend: KeystoneBackend, sandbox: FakeSandbox
) -> None:
    await run(backend, sandbox, "run_command", command="deploy now")

    ran = await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_2")

    assert ran.approval_question is not None
    assert "apr-1" in ran.approval_question


# ---------- the session ----------
async def test_the_task_id_is_the_session(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    await run(backend, sandbox, "run_command", task="lh-task-1", command="echo 1")
    await run(backend, sandbox, "read_file", task="lh-task-1", path="notes.txt")
    await run(backend, sandbox, "run_command", task="lh-task-2", command="echo 2")

    first, second, other = keystone.sessions()
    assert first is not None
    # One Keystone session for all the calls of a task, so its flags and taint follow the task.
    assert first == second
    assert other is not None and other != first
    # And the task id travels in the call, so the audit log can tie a call to its task.
    assert keystone.calls[0].arguments[CONTEXT_KEY]["task_id"] == "lh-task-1"
    assert keystone.calls[2].arguments[CONTEXT_KEY]["task_id"] == "lh-task-2"


async def test_the_call_carries_what_the_worker_needs_to_find_the_sandbox(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    await run(backend, sandbox, "run_command", command="echo hi")

    (seen,) = keystone.calls
    assert seen.arguments["command"] == "echo hi"
    assert seen.arguments[CONTEXT_KEY] == {
        "task_id": "lh-task-1",
        "sandbox_id": "sbx-1",
        "turn_index": 3,
        "output_max_tokens": 4000,
    }


async def test_concurrent_first_calls_of_a_task_open_one_session(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    import asyncio

    await asyncio.gather(
        *(
            run(backend, sandbox, "run_command", command=f"echo {n}", call_id=f"c{n}")
            for n in range(4)
        )
    )

    assert len(set(keystone.sessions())) == 1


async def test_a_new_session_is_opened_after_the_connection_failed(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    await run(backend, sandbox, "run_command", command="echo 1")
    keystone.broken = True
    with pytest.raises(Exception):  # noqa: B017 - any failure to reach the gateway is raised
        await run(backend, sandbox, "run_command", command="echo 2")
    keystone.broken = False

    again = await run(backend, sandbox, "run_command", command="echo 3")

    assert again.result.ok
    first, _, last = keystone.sessions()
    assert first != last


async def test_a_session_is_closed_when_the_backend_is(
    keystone: FakeKeystone, sandbox: FakeSandbox
) -> None:
    found = KeystoneBackend(keystone.url, token=lambda: "t")
    await run(found, sandbox, "run_command", command="echo 1")

    await found.aclose()
    await found.aclose()  # closing twice is fine

    ran = await run(found, sandbox, "run_command", command="echo 2")
    assert ran.result.ok  # a closed backend opens a new session on the next call
    await found.aclose()


# ---------- the credential ----------
async def test_the_bearer_token_is_read_for_every_request(
    keystone: FakeKeystone, sandbox: FakeSandbox
) -> None:
    tokens = iter(["first", "second", "third", "fourth", "fifth", "sixth"])
    found = KeystoneBackend(keystone.url, token=lambda: next(tokens))
    try:
        await run(found, sandbox, "run_command", command="echo 1")
        await run(found, sandbox, "run_command", command="echo 2")
    finally:
        await found.aclose()

    sent = [seen.authorization for seen in keystone.calls]
    assert sent[0] != sent[1]
    assert all(value is not None and value.startswith("Bearer ") for value in sent)


async def test_a_gateway_that_is_down_raises_so_that_the_activity_is_retried(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    keystone.broken = True

    with pytest.raises(Exception):  # noqa: B017 - the type depends on the transport
        await run(backend, sandbox, "run_command", command="echo hi")


# ---------- what stays local ----------
@pytest.mark.parametrize("name", ["finish", "read_output"])
async def test_control_and_output_tools_never_leave_the_worker(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone, name: str
) -> None:
    arguments = {"summary": "done"} if name == "finish" else {"handle": "out_none"}

    ran = await run(backend, sandbox, name, **arguments)

    assert keystone.calls == []
    assert ran.approval_question is None
    assert ran.result.call_id == "toolu_1"


async def test_an_unknown_tool_is_answered_without_asking_keystone(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    ran = await run(backend, sandbox, "no_such_tool")

    assert not ran.result.ok
    assert "unknown tool" in ran.result.output
    assert keystone.calls == []


# ---------- direct mode ----------
async def test_the_direct_backend_runs_the_registry_as_before(sandbox: FakeSandbox) -> None:
    await sandbox.write_file("notes.txt", b"one\ntwo\n")
    call = ToolCall(id="toolu_9", name="read_file", input={"path": "notes.txt"})
    ctx = ToolContext(task_id="t", turn_index=0, blob_prefix="tasks/t")
    registry = default_registry()

    ran = await DirectBackend().run(registry, sandbox, call, ctx, sandbox_id=sandbox.id)
    plain = await registry.run(call, sandbox, ctx)

    assert ran.approval_question is None
    assert ran.result.model_dump(exclude={"duration_ms"}) == plain.model_dump(
        exclude={"duration_ms"}
    )
    assert DirectBackend.remote is False
    assert KeystoneBackend.remote is True
