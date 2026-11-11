"""The Keystone tool backend (LH-909).

With `tools.backend: keystone` the worker calls each sandbox tool through Keystone's MCP
endpoint, and Keystone decides it. These tests run the backend against a fake Keystone, an MCP
endpoint on this machine that allows, denies or holds a call the way the real one answers
(tests/fakes/fake_keystone.py). The mapping under test: an allow is the tool's own result, a
deny is a failed result with Keystone's reason, a call held for approval is a failed result that
also asks for a pause, and the task id keeps every call of one task in one Keystone session.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncIterator

import pytest
from tests.fakes.fake_keystone import AWAIT_TOOL, FakeKeystone, running_keystone

from longhaul.contracts import ToolCall, ToolContext
from longhaul.sandbox.local_manager import FakeSandbox
from longhaul.tools.backend import DirectBackend, Ran
from longhaul.tools.keystone_backend import KeystoneBackend, KeystoneFault
from longhaul.tools.mcp_server import CONTEXT_KEY
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


# ---------- an approval that cannot be used ----------
async def test_an_approval_from_a_session_that_is_gone_is_said_so_and_asked_again(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    await run(backend, sandbox, "run_command", command="deploy now")
    keystone.decide("apr-1", "approved")
    # The worker restarted, or the session was idle too long: the next call opens a new one.
    await backend.aclose()

    refused = await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_2")

    assert not refused.result.ok
    assert refused.approval_question is None
    assert "KS_DENIED" in refused.result.output
    # The model is told the approval was lost and what asking again does, not left to guess.
    assert "approval" in refused.result.output and "could not be used" in refused.result.output
    again = await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_3")
    assert keystone.calls[-1].tool == "longhaul_sandbox__run_command"
    assert again.approval_question is not None
    assert "apr-2" in again.approval_question


async def test_an_expired_approval_is_said_so_and_asked_again(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    await run(backend, sandbox, "run_command", command="deploy now")
    keystone.decide("apr-1", "approved")
    keystone.expire("apr-1")

    refused = await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_2")

    assert not refused.result.ok
    assert refused.approval_question is None
    assert "KS_APPROVAL_EXPIRED" in refused.result.output
    assert "could not be used" in refused.result.output
    again = await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_3")
    assert keystone.calls[-1].tool == "longhaul_sandbox__run_command"
    assert again.approval_question is not None


async def test_a_failed_await_keeps_the_request_for_the_retry(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    await run(backend, sandbox, "run_command", command="deploy now")
    keystone.decide("apr-1", "approved")
    keystone.broken = True
    with pytest.raises(Exception):  # noqa: B017 - the type depends on the transport
        await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_2")
    keystone.broken = False

    await run(backend, sandbox, "run_command", command="deploy now", call_id="toolu_2")

    # The activity retry settles the same request. Sending the original call again would hold it
    # a second time and pause the task for nothing.
    assert [seen.tool for seen in keystone.calls] == [
        "longhaul_sandbox__run_command",
        AWAIT_TOOL,
        AWAIT_TOOL,
    ]


# ---------- faults of the upstream ----------
async def test_an_upstream_error_is_raised_so_the_activity_is_retried(
    backend: KeystoneBackend, sandbox: FakeSandbox
) -> None:
    # The worker's server failed or could not be reached. That is not the model's doing, and a
    # result would count toward the stuck rule instead of letting Temporal try again.
    with pytest.raises(KeystoneFault, match="KS_UPSTREAM_ERROR"):
        await run(backend, sandbox, "run_command", command="explode")


async def test_an_upstream_timeout_is_raised_and_names_the_setting(
    backend: KeystoneBackend, sandbox: FakeSandbox
) -> None:
    with pytest.raises(KeystoneFault, match="KS_UPSTREAM_TIMEOUT") as caught:
        await run(backend, sandbox, "run_command", command="stall")

    # The command may have run. The retry starts from the checkpoint, and the message says what
    # to raise when the cause is a timeout that is too short.
    assert "timeout_s" in str(caught.value)


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


async def test_the_test_command_of_the_task_travels_with_a_run_tests_call(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    # The server behind Keystone is built once for the worker. The task's command reaches it
    # only through the context, so run_tests runs the same command as in direct mode.
    registry = default_registry().with_test_command("make check")
    call = ToolCall(id="toolu_1", name="run_tests", input={})
    ctx = ToolContext(task_id="lh-task-1", turn_index=3, blob_prefix="tasks/lh-task-1")

    await backend.run(registry, sandbox, call, ctx, sandbox_id=sandbox.id)

    (seen,) = keystone.calls
    assert seen.arguments[CONTEXT_KEY]["test_command"] == "make check"
    assert "test_command" not in seen.arguments


async def test_a_call_other_than_run_tests_carries_no_test_command(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    registry = default_registry().with_test_command("make check")
    call = ToolCall(id="toolu_1", name="run_command", input={"command": "echo hi"})
    ctx = ToolContext(task_id="lh-task-1", turn_index=3, blob_prefix="tasks/lh-task-1")

    await backend.run(registry, sandbox, call, ctx, sandbox_id=sandbox.id)

    (seen,) = keystone.calls
    assert "test_command" not in seen.arguments[CONTEXT_KEY]


async def test_the_workers_context_wins_over_one_the_model_sends(
    backend: KeystoneBackend, sandbox: FakeSandbox, keystone: FakeKeystone
) -> None:
    # A prompt-injected model must not point the worker's server at another task's sandbox.
    forged = {"task_id": "lh-other", "sandbox_id": "sbx-other", "turn_index": 0}

    await run(backend, sandbox, "run_command", command="echo hi", **{CONTEXT_KEY: forged})

    (seen,) = keystone.calls
    assert seen.arguments[CONTEXT_KEY]["task_id"] == "lh-task-1"
    assert seen.arguments[CONTEXT_KEY]["sandbox_id"] == "sbx-1"


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
    # A call is more than one HTTP request (the handshake, the call, the close), so the source
    # hands out a new token every time it is asked.
    count = itertools.count()
    found = KeystoneBackend(keystone.url, token=lambda: f"token-{next(count)}")
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
