"""The console API against a seeded database and a time skipping Temporal server (LH-801).

Every test builds the FastAPI app on the objects a deployment would give it: sessions on a
throwaway Postgres database that the test creates, migrates and drops (the fixtures of
test_run_report.py), an in-memory blob store, the memory store with the hash embedder, and a
Temporal client. Requests go through httpx's ASGI transport, so no port is opened.

The workflow side uses the stubs of test_task_workflow.py: TaskWorkflow runs for real, its
activities are stand-ins, and a held `prepare_workspace` keeps a task open long enough to query
and signal it. A signal is checked where it lands. The hint shows up in the input of the next
model call, the cancel in the outcome, and the other three in the history of the workflow, where
the name and the arguments of every signal are recorded.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowHandle
from temporalio.testing import WorkflowEnvironment
from tests.integration import test_run_report as report_tests
from tests.integration import test_task_workflow as workflow_tests
from tests.integration.test_run_report import REPO, TASK, a_plan, at
from tests.integration.test_task_workflow import (
    SHORT_HEARTBEAT,
    Stubs,
    make_spec,
    result_of,
    running,
    wait_for_phase,
    wait_until,
)

from longhaul.api import routes_tasks
from longhaul.api.app import create_app
from longhaul.api.deps import ApiContext
from longhaul.blobs.base import blob_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    LLMResponse,
    MemoryItem,
    Message,
    Outcome,
    OutcomeKind,
    Phase,
    TaskStatus,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from longhaul.db.models import Event, PlanRow, RepoSetting, Task, ToolCallRow
from longhaul.db.records import SqlRecorder, TurnRecord
from longhaul.db.session import make_engine
from longhaul.durable import acts
from longhaul.durable.client import start_task
from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.memory.embedder import HashEmbedder
from longhaul.memory.store import PgMemoryStore

pytestmark = [pytest.mark.integration, pytest.mark.phase8]

# The scratch database, its sessions and the time skipping server are fixtures of the two modules
# above. Binding them to names here makes pytest offer them to the tests of this file.
database = report_tests.database
sessions = report_tests.sessions
env = workflow_tests.env

Sessions = async_sessionmaker[AsyncSession]

# Built from pieces so that no secret-looking literal sits in the source.
OPENAI_KEY = "sk-" + "a1B2c3D4" * 4
GITHUB_TOKEN = "ghp_" + "Z9y8X7w6V5" * 4
REDACTED = "[redacted]"
HINT = "the tests import the copy in vendor/, edit that one"


# ---------- the app ----------
class Stack:
    """What a test needs to seed and to look at: the app's client and the stores behind it."""

    def __init__(
        self, client: httpx2.AsyncClient, sessions: Sessions, blobs: MemoryBlobStore
    ) -> None:
        self.client = client
        self.sessions = sessions
        self.blobs = blobs

    async def get(self, path: str, **params: Any) -> httpx2.Response:
        return await self.client.get(path, params=params)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> httpx2.Response:
        return await self.client.post(path, json=body)


@asynccontextmanager
async def serving(sessions: Sessions, temporal: Client | None = None) -> AsyncIterator[Stack]:
    """The app on a scratch database. With no Temporal client, any call to Temporal fails."""
    blobs = MemoryBlobStore()

    async def connect() -> Client:
        if temporal is None:
            raise ConnectionError("cannot reach Temporal at localhost:1")
        return temporal

    context = ApiContext(
        sessions=sessions,
        blobs=blobs,
        memory=PgMemoryStore(sessions, HashEmbedder()),
        temporal=connect,
    )
    transport = httpx2.ASGITransport(app=create_app(context))
    async with httpx2.AsyncClient(transport=transport, base_url="http://api") as client:
        yield Stack(client, sessions, blobs)


@pytest.fixture
async def api(sessions: Sessions) -> AsyncIterator[Stack]:
    async with serving(sessions) as stack:
        yield stack


# ---------- seeding ----------
async def add_task(sessions: Sessions, task_id: str, **fields: Any) -> None:
    values: dict[str, Any] = {
        "id": task_id,
        "repo": REPO,
        "issue_number": 7,
        "title": "Reservations can drive stock negative",
        "status": "active",
        "phase": "coding",
        "started_at": at(0),
        "created_at": at(0),
    }
    async with sessions.begin() as session:
        session.add(Task(**{**values, **fields}))


async def add_event(sessions: Sessions, seconds: int, type: str, **payload: Any) -> None:
    async with sessions.begin() as session:
        session.add(Event(task_id=TASK, ts=at(seconds), type=type, payload=payload))


async def add_turn(
    sessions: Sessions, index: int, attempt: int, seconds: int, **fields: Any
) -> None:
    await SqlRecorder(sessions).record_turn(
        TurnRecord(
            task_id=fields.pop("task_id", TASK),
            turn_index=index,
            attempt=attempt,
            role="coder",
            started_at=at(seconds),
            usage=Usage(input_tokens=100, output_tokens=20),
            stop_reason="tool_use",
            **fields,
        )
    )


async def add_call(
    sessions: Sessions, call_id: str, seconds: int, *, task_id: str = TASK, **fields: Any
) -> None:
    values: dict[str, Any] = {
        "id": call_id,
        "task_id": task_id,
        "turn_index": 0,
        "name": "run_command",
        "ok": True,
        "created_at": at(seconds),
    }
    async with sessions.begin() as session:
        session.add(ToolCallRow(**{**values, **fields}))


# ---------- GET /api/tasks ----------
async def test_tasks_list_filters(api: Stack) -> None:
    # Two tasks share a creation time, so the order between them has to come from the id.
    await add_task(api.sessions, "lh-acme-calc-1", repo="acme/calc", created_at=at(10))
    await add_task(api.sessions, "lh-acme-calc-2", repo="acme/calc", created_at=at(20))
    await add_task(
        api.sessions, "lh-acme-calc-3", repo="acme/calc", status="done", created_at=at(30)
    )
    await add_task(api.sessions, "lh-acme-inv-1", repo="acme/inventory", created_at=at(30))
    await add_task(
        api.sessions, "lh-acme-inv-2", repo="acme/inventory", status="done", created_at=at(40)
    )

    everything = await api.get("/api/tasks")
    assert everything.status_code == 200
    body = everything.json()
    assert [t["id"] for t in body["items"]] == [
        "lh-acme-inv-2",
        "lh-acme-inv-1",
        "lh-acme-calc-3",
        "lh-acme-calc-2",
        "lh-acme-calc-1",
    ]
    assert body["next_cursor"] is None
    first = body["items"][-1]
    assert (first["repo"], first["issue_number"], first["status"], first["phase"]) == (
        "acme/calc",
        7,
        "active",
        "coding",
    )

    by_repo = await api.get("/api/tasks", repo="acme/inventory")
    assert [t["id"] for t in by_repo.json()["items"]] == ["lh-acme-inv-2", "lh-acme-inv-1"]
    by_status = await api.get("/api/tasks", status="done")
    assert [t["id"] for t in by_status.json()["items"]] == ["lh-acme-inv-2", "lh-acme-calc-3"]
    both = await api.get("/api/tasks", status="done", repo="acme/calc")
    assert [t["id"] for t in both.json()["items"]] == ["lh-acme-calc-3"]
    assert (await api.get("/api/tasks", repo="acme/none")).json() == {
        "items": [],
        "next_cursor": None,
    }


async def test_tasks_list_pages_with_a_cursor(api: Stack) -> None:
    for n in range(5):
        await add_task(api.sessions, f"lh-acme-calc-{n}", created_at=at(n // 2))

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = (await api.get("/api/tasks", **params)).json()
        seen += [t["id"] for t in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break

    assert pages == 3
    assert seen == [f"lh-acme-calc-{n}" for n in (4, 3, 2, 1, 0)]


async def test_tasks_list_refuses_bad_input(api: Stack) -> None:
    assert (await api.get("/api/tasks", limit=0)).status_code == 422
    assert (await api.get("/api/tasks", limit=201)).status_code == 422
    assert (await api.get("/api/tasks", cursor="not-a-cursor")).status_code == 400


# ---------- GET /api/tasks/{id} ----------
async def test_task_detail_live_status(env: WorkflowEnvironment, sessions: Sessions) -> None:
    spec = make_spec(f"lh-acme-calc-{uuid.uuid4().hex[:8]}")
    await add_task(sessions, spec.task_id, repo="acme/calc", issue_number=None, phase="queued")
    async with sessions.begin() as session:
        session.add(
            PlanRow(task_id=spec.task_id, version=1, plan=a_plan().model_dump(), status="proposed")
        )
        session.add(
            PlanRow(
                task_id=spec.task_id,
                version=2,
                plan=a_plan().model_copy(update={"summary": "Second try"}).model_dump(),
                status="approved",
                decided_by="alice",
            )
        )
    stubs = Stubs()
    stubs.hold(acts.PREPARE_WORKSPACE.name)
    async with running(env, stubs) as queue, serving(sessions, env.client) as api:
        handle = await start_task(env.client, spec, task_queue=queue)
        await wait_for_phase(handle, Phase.PREPARING)

        live = (await api.get(f"/api/tasks/{spec.task_id}")).json()
        stubs.release(acts.PREPARE_WORKSPACE.name)
        outcome = await result_of(handle)
        closed = (await api.get(f"/api/tasks/{spec.task_id}")).json()

        # A row whose workflow Temporal does not know: the row alone is the answer.
        await add_task(sessions, "lh-acme-calc-gone", repo="acme/calc")
        gone = await api.get("/api/tasks/lh-acme-calc-gone")

    assert live["task"]["id"] == spec.task_id
    assert live["task"]["repo"] == "acme/calc"
    # The row still says what the poller wrote. The phase that is current comes from the query.
    assert live["task"]["phase"] == "queued"
    assert live["workflow"] == "running"
    assert live["live"]["phase"] == "preparing"
    assert live["live"]["task_id"] == spec.task_id
    # The newest version of the plan, with who decided on it.
    assert live["plan"]["version"] == 2
    assert live["plan"]["status"] == "approved"
    assert live["plan"]["decided_by"] == "alice"
    assert live["plan"]["plan"]["summary"] == "Second try"
    assert len(live["plan"]["plan"]["steps"]) == 3

    assert closed["workflow"] == "closed"
    assert closed["live"]["phase"] == "done"
    assert closed["live"]["outcome"]["kind"] == outcome.kind.value

    assert gone.status_code == 200
    assert gone.json()["workflow"] == "not_found"
    assert gone.json()["live"] is None
    assert gone.json()["plan"] is None


async def test_task_detail_without_temporal_or_row(api: Stack) -> None:
    await add_task(api.sessions, TASK)

    detail = await api.get(f"/api/tasks/{TASK}")
    missing = await api.get("/api/tasks/lh-nobody-nothing-1")

    # The page still has its row when Temporal is down, and says why it has no live status.
    assert detail.status_code == 200
    assert detail.json()["workflow"] == "unreachable"
    assert detail.json()["live"] is None
    assert detail.json()["task"]["title"] == "Reservations can drive stock negative"
    assert missing.status_code == 404


# ---------- GET /api/tasks/{id}/timeline ----------
def label(item: dict[str, Any]) -> str:
    kind = item["kind"]
    if kind == "event":
        return f"event:{item['type']}"
    if kind == "turn":
        return f"turn:{item['turn_index']}/{item['attempt']}"
    return f"call:{item['id']}"


async def seed_timeline(sessions: Sessions) -> None:
    await add_task(sessions, TASK)
    await add_task(sessions, "lh-acme-other-1")
    await add_event(sessions, 30, "paused", reason="stuck")
    await add_event(sessions, 5, "phase_changed", phase="coding")
    await add_event(sessions, 12, "hint", by="alice")
    await add_turn(sessions, 1, 2, 20)
    await add_turn(sessions, 0, 1, 10)
    await add_turn(sessions, 1, 1, 20)
    await add_call(sessions, "toolu_b", 12)
    await add_call(sessions, "toolu_a", 12)
    await add_call(sessions, "toolu_c", 25, turn_index=1, ok=False, exit_code=1)
    # Rows of another task sit between the times and must not show.
    await add_turn(sessions, 0, 1, 11, task_id="lh-acme-other-1")
    await add_call(sessions, "toolu_other", 13, task_id="lh-acme-other-1")


# A tie in time is broken by the kind (turn, tool call, event) and then by the key of the row.
TIMELINE = [
    "event:phase_changed",
    "turn:0/1",
    "call:toolu_a",
    "call:toolu_b",
    "event:hint",
    "turn:1/1",
    "turn:1/2",
    "call:toolu_c",
    "event:paused",
]


async def test_timeline_order(api: Stack) -> None:
    await seed_timeline(api.sessions)

    full = (await api.get(f"/api/tasks/{TASK}/timeline")).json()

    assert [label(i) for i in full["items"]] == TIMELINE
    assert full["next_cursor"] is None
    by_label = {label(i): i for i in full["items"]}
    assert by_label["event:paused"]["payload"] == {"reason": "stuck"}
    assert by_label["turn:0/1"]["role"] == "coder"
    assert by_label["turn:0/1"]["input_tokens"] == 100
    assert by_label["call:toolu_c"]["ok"] is False
    assert by_label["call:toolu_c"]["exit_code"] == 1
    assert by_label["call:toolu_c"]["name"] == "run_command"
    assert [i["ts"] for i in full["items"]] == sorted(i["ts"] for i in full["items"])

    # The same list, four rows at a time. Nothing repeats and nothing is skipped at a seam, also
    # where the seam falls inside a tie.
    for size in (1, 2, 4):
        paged: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": size}
            if cursor:
                params["cursor"] = cursor
            page = (await api.get(f"/api/tasks/{TASK}/timeline", **params)).json()
            paged += [label(i) for i in page["items"]]
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert paged == TIMELINE, f"page size {size}"


async def test_timeline_refuses_bad_input(api: Stack) -> None:
    await seed_timeline(api.sessions)

    assert (await api.get(f"/api/tasks/{TASK}/timeline", cursor="nonsense")).status_code == 400
    assert (await api.get(f"/api/tasks/{TASK}/timeline", limit=0)).status_code == 422
    assert (await api.get("/api/tasks/lh-nobody-nothing-1/timeline")).status_code == 404


# ---------- turns and outputs, redacted ----------
async def seed_turn(stack: Stack) -> None:
    await add_task(stack.sessions, TASK)
    await add_task(stack.sessions, "lh-acme-other-1")
    request = [
        Message(role="user", content=[TextBlock(text=f"Fix it. The key is {OPENAI_KEY}.")]),
        Message(
            role="assistant",
            content=[
                ToolUseBlock(
                    id="toolu_1",
                    name="run_command",
                    input={"command": f"curl -H 'token: {GITHUB_TOKEN}' https://x", "n": 3},
                )
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="toolu_1", content=f"ok {GITHUB_TOKEN}")],
        ),
    ]
    response = LLMResponse(
        content=[TextBlock(text=f"I will run it with {OPENAI_KEY}")],
        tool_calls=[ToolCall(id="toolu_2", name="run_command", input={"command": OPENAI_KEY})],
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=20),
        model="claude-sonnet-5-5",
        latency_ms=900,
    )
    request_ref = await stack.blobs.put_json(
        request, blob_key(TASK, "turns", 3, "transcript-1.json")
    )
    response_ref = await stack.blobs.put_json(
        response, blob_key(TASK, "turns", 3, "response-2.json")
    )
    await add_turn(stack.sessions, 3, 1, 10, request_ref=request_ref.key)
    await add_turn(
        stack.sessions, 3, 2, 12, request_ref=request_ref.key, response_ref=response_ref.key
    )
    # The whole output of a call: line 3 holds a token, and the text is longer than one page.
    lines = [f"line {n}" for n in range(1, 11)]
    lines[2] = f"Authorization: {GITHUB_TOKEN}"
    output_ref = await stack.blobs.put(
        "\n".join(lines).encode(), key=blob_key(TASK, "turns", 3, "tool-toolu_1-output.txt")
    )
    await add_call(stack.sessions, "toolu_1", 11, turn_index=3, output_ref=output_ref.key)
    other_ref = await stack.blobs.put(
        b"belongs to another task", key=blob_key("lh-acme-other-1", "turns", 3, "out.txt")
    )
    await add_call(
        stack.sessions, "toolu_x", 11, task_id="lh-acme-other-1", output_ref=other_ref.key
    )
    await add_event(stack.sessions, 15, "stuck", note=f"saw {OPENAI_KEY}")


async def test_outputs_redacted(api: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_turn(api)
    leaky = f"export OPENAI_API_KEY={OPENAI_KEY}"
    async with api.sessions.begin() as session:
        session.add(
            PlanRow(
                task_id=TASK,
                version=1,
                plan=a_plan().model_copy(update={"summary": leaky}).model_dump(),
                status="proposed",
            )
        )

    async def leaky_status(ctx: Any, task_id: str) -> tuple[str, TaskStatus]:
        status = TaskStatus(
            task_id=task_id,
            phase=Phase.CODING,
            turn_index=3,
            cost_usd=0.1,
            last_checkpoint=None,
            plan_version=1,
            paused_reason=f"asked about {leaky}",
            outcome=Outcome(kind=OutcomeKind.STUCK, summary=f"gave up with {leaky}"),
        )
        return "running", status

    monkeypatch.setattr(routes_tasks, "live_status", leaky_status)
    detail = await api.get(f"/api/tasks/{TASK}")

    latest = await api.get(f"/api/tasks/{TASK}/turns/3")
    first = await api.get(f"/api/tasks/{TASK}/turns/3", attempt=1)
    output = await api.get(f"/api/tasks/{TASK}/tool-calls/toolu_1/output")
    timeline = await api.get(f"/api/tasks/{TASK}/timeline")

    # The blobs hold the secrets as the model saw them. Redaction happens when they are read.
    stored = (await api.blobs.get_by_key(blob_key(TASK, "turns", 3, "response-2.json"))).decode()
    assert OPENAI_KEY in stored

    assert detail.status_code == 200
    body = detail.json()
    assert body["plan"]["plan"]["summary"] == f"export OPENAI_API_KEY={REDACTED}"
    assert body["live"]["paused_reason"] == f"asked about export OPENAI_API_KEY={REDACTED}"
    assert body["live"]["outcome"]["summary"] == f"gave up with export OPENAI_API_KEY={REDACTED}"

    for response in (detail, latest, first, output, timeline):
        assert response.status_code == 200
        assert OPENAI_KEY not in response.text
        assert GITHUB_TOKEN not in response.text

    turn = latest.json()
    assert (turn["turn_index"], turn["attempt"], turn["attempts"]) == (3, 2, [1, 2])
    assert turn["role"] == "coder"
    request = turn["request"]
    assert request[0]["content"][0]["text"] == f"Fix it. The key is {REDACTED}."
    assert request[1]["content"][0]["input"]["command"] == f"curl -H 'token: {REDACTED}' https://x"
    assert request[1]["content"][0]["input"]["n"] == 3
    assert request[2]["content"][0]["content"] == f"ok {REDACTED}"
    assert turn["response"]["content"][0]["text"] == f"I will run it with {REDACTED}"
    assert turn["response"]["tool_calls"][0]["input"] == {"command": REDACTED}
    assert turn["response"]["usage"]["output_tokens"] == 20

    # Attempt 1 was billed and failed: it has the request and no reply.
    assert first.json()["attempt"] == 1
    assert first.json()["request"] is not None
    assert first.json()["response"] is None

    page = output.json()
    assert page["call_id"] == "toolu_1"
    assert page["total_lines"] == 10
    assert (page["start_line"], page["end_line"]) == (1, 10)
    assert page["text"].splitlines()[2] == f"Authorization: {REDACTED}"
    assert page["next_start"] is None

    event = next(i for i in timeline.json()["items"] if i["kind"] == "event")
    assert event["payload"] == {"note": f"saw {REDACTED}"}


async def test_output_range_and_scope(api: Stack) -> None:
    await seed_turn(api)
    path = f"/api/tasks/{TASK}/tool-calls/toolu_1/output"

    middle = (await api.get(path, start=4, end=6)).json()
    tail = (await api.get(path, start=9)).json()
    backwards = await api.get(path, start=6, end=4)
    beyond = await api.get(path, start=11)

    assert middle["text"] == "line 4\nline 5\nline 6"
    assert (middle["start_line"], middle["end_line"], middle["total_lines"]) == (4, 6, 10)
    assert tail["text"] == "line 9\nline 10"
    assert tail["end_line"] == 10
    assert backwards.status_code == 422
    assert beyond.status_code == 416

    # A call is found through its own task only.
    assert (await api.get(f"/api/tasks/{TASK}/tool-calls/toolu_x/output")).status_code == 404
    assert (await api.get(f"/api/tasks/{TASK}/tool-calls/toolu_none/output")).status_code == 404
    assert (await api.get("/api/tasks/lh-acme-other-1/turns/3")).status_code == 404
    assert (await api.get(f"/api/tasks/{TASK}/turns/99")).status_code == 404
    assert (await api.get(f"/api/tasks/{TASK}/turns/3", attempt=7)).status_code == 404


# ---------- POST /api/tasks/{id}/... ----------
async def signals_of(handle: WorkflowHandle[Any, Any]) -> list[tuple[str, list[Any]]]:
    """Every signal the workflow received, in order, with its decoded arguments."""
    history = await handle.fetch_history()
    found: list[tuple[str, list[Any]]] = []
    for event in history.events:
        if event.HasField("workflow_execution_signaled_event_attributes"):
            attrs = event.workflow_execution_signaled_event_attributes
            args = await guarded_data_converter.decode(list(attrs.input.payloads))
            found.append((attrs.signal_name, args))
    return found


async def test_actions_signal(env: WorkflowEnvironment, sessions: Sessions) -> None:
    spec = make_spec(f"lh-acme-calc-{uuid.uuid4().hex[:8]}")
    stubs = Stubs()
    stubs.hold(acts.PREPARE_WORKSPACE.name)
    async with running(env, stubs) as queue, serving(sessions, env.client) as api:
        handle = await start_task(env.client, spec, task_queue=queue)
        await wait_for_phase(handle, Phase.PREPARING)
        base = f"/api/tasks/{spec.task_id}"

        approved = await api.post(f"{base}/approve-plan")
        rejected = await api.post(f"{base}/reject-plan", {"feedback": "split step 2"})
        budget = await api.post(f"{base}/budget", {"usd": 2.5, "turns": 10})
        hinted = await api.post(f"{base}/hint", {"text": HINT})
        stubs.release(acts.PREPARE_WORKSPACE.name)
        await result_of(handle)
        after_the_end = await api.post(f"{base}/hint", {"text": HINT})
        unknown = await api.post("/api/tasks/lh-nobody-nothing-1/approve-plan")

        signals = await signals_of(handle)

    assert approved.status_code == rejected.status_code == budget.status_code == 200
    assert hinted.status_code == 200
    assert approved.json() == {"task_id": spec.task_id, "signal": "approve_plan", "by": "console"}
    assert hinted.json()["signal"] == "hint"
    assert signals == [
        ("approve_plan", ["console"]),
        ("reject_plan", ["console", "split step 2"]),
        ("add_budget", ["console", 2.5, 10]),
        ("hint", ["console", HINT]),
    ]
    # The hint is in the state of the workflow: the first model call after it carries it.
    first_turn = stubs.payloads(acts.LLM_TURN.name)[0]
    assert first_turn.hints == [f"console: {HINT}"]
    # A task that has ended and a task that never existed are told apart from a server error.
    assert after_the_end.status_code == 409
    assert unknown.status_code == 404


async def test_cancel_action_ends_the_task(env: WorkflowEnvironment, sessions: Sessions) -> None:
    spec = make_spec(f"lh-acme-calc-{uuid.uuid4().hex[:8]}")
    stubs = Stubs()
    stubs.hold(acts.PREPARE_WORKSPACE.name)  # never opened: the cancel has to end the run
    async with running(env, stubs) as queue, serving(sessions, env.client) as api:
        handle = await start_task(env.client, spec, task_queue=queue, tuning=SHORT_HEARTBEAT)
        await wait_for_phase(handle, Phase.PREPARING)
        await wait_until(lambda: acts.PREPARE_WORKSPACE.name in stubs.names(), "prepare to start")

        response = await api.post(f"/api/tasks/{spec.task_id}/cancel", {"reason": "wrong repo"})
        outcome = await result_of(handle)

    assert response.status_code == 200
    assert response.json()["signal"] == "cancel"
    assert outcome.kind is OutcomeKind.CANCELLED
    assert outcome.summary == "cancelled by console: wrong repo"


async def test_actions_refuse_bad_bodies(env: WorkflowEnvironment, sessions: Sessions) -> None:
    spec = make_spec(f"lh-acme-calc-{uuid.uuid4().hex[:8]}")
    stubs = Stubs()
    stubs.hold(acts.PREPARE_WORKSPACE.name)
    async with running(env, stubs) as queue, serving(sessions, env.client) as api:
        handle = await start_task(env.client, spec, task_queue=queue)
        await wait_for_phase(handle, Phase.PREPARING)
        base = f"/api/tasks/{spec.task_id}"

        empty_hint = await api.post(f"{base}/hint", {"text": "   "})
        no_hint = await api.post(f"{base}/hint", {})
        nothing = await api.post(f"{base}/budget", {"usd": 0, "turns": 0})
        negative = await api.post(f"{base}/budget", {"usd": -1, "turns": 0})
        no_feedback = await api.post(f"{base}/reject-plan", {})
        signals = await signals_of(handle)

    assert [r.status_code for r in (empty_hint, no_hint, nothing, negative, no_feedback)] == [
        422
    ] * 5
    assert signals == []


async def test_actions_report_unreachable_temporal(api: Stack) -> None:
    response = await api.post(f"/api/tasks/{TASK}/cancel", {"reason": "stop"})

    assert response.status_code == 503


# ---------- /api/memory ----------
async def seed_memory(stack: Stack) -> dict[str, str]:
    store = PgMemoryStore(stack.sessions, HashEmbedder())

    def item(kind: Any, content: str, repo: str = REPO) -> MemoryItem:
        return MemoryItem(
            id="x", repo=repo, kind=kind, content=content, status="candidate", source="task"
        )

    return {
        "lesson": await store.add_candidate(
            item("lesson", f"Run pytest -q first. Never paste {OPENAI_KEY}."), source_ref=TASK
        ),
        "pitfall": await store.add_candidate(item("pitfall", "Stock can go negative.")),
        "other": await store.add_candidate(item("lesson", "Another repo.", "acme/other")),
        "command": await store.add_verified(
            MemoryItem(
                id="x",
                repo=REPO,
                kind="command",
                content="pytest -q",
                status="approved",
                source="default_branch",
            ),
            source_ref="a" * 40,
        ),
    }


async def test_memory_list_filters(api: Stack) -> None:
    ids = await seed_memory(api)

    every = (await api.get("/api/memory", repo=REPO)).json()
    candidates = (await api.get("/api/memory", repo=REPO, status="candidate")).json()
    lessons = (await api.get("/api/memory", repo=REPO, kind="lesson")).json()
    anywhere = (await api.get("/api/memory")).json()
    nothing = (await api.get("/api/memory", repo=REPO, status="rejected")).json()

    assert {i["id"] for i in every} == {ids["lesson"], ids["pitfall"], ids["command"]}
    assert {i["id"] for i in candidates} == {ids["lesson"], ids["pitfall"]}
    assert [i["id"] for i in lessons] == [ids["lesson"]]
    assert len(anywhere) == 4
    assert nothing == []
    lesson = lessons[0]
    assert lesson["status"] == "candidate"
    assert lesson["source"] == "task"
    assert lesson["source_ref"] == TASK
    assert lesson["uses"] == 0
    assert lesson["content"] == f"Run pytest -q first. Never paste {REDACTED}."
    command = next(i for i in every if i["kind"] == "command")
    assert (command["status"], command["approved_by"]) == ("approved", "default_branch")
    assert "embedding" not in command

    assert (await api.get("/api/memory", status="maybe")).status_code == 422
    assert (await api.get("/api/memory", kind="note")).status_code == 422


async def test_memory_approve_and_reject(api: Stack) -> None:
    ids = await seed_memory(api)

    approved = await api.post(f"/api/memory/{ids['lesson']}/approve")
    rejected = await api.post(f"/api/memory/{ids['pitfall']}/reject")
    # A person can change their mind: approving a rejected item is allowed, as in the CLI.
    again = await api.post(f"/api/memory/{ids['pitfall']}/approve")
    listed = {i["id"]: i for i in (await api.get("/api/memory", repo=REPO)).json()}

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["approved_by"] == "console"
    assert rejected.json()["status"] == "rejected"
    assert rejected.json()["approved_by"] is None
    assert again.json()["status"] == "approved"
    assert listed[ids["lesson"]]["status"] == "approved"
    assert listed[ids["pitfall"]]["approved_by"] == "console"

    assert (await api.post(f"/api/memory/{uuid.uuid4()}/approve")).status_code == 404
    assert (await api.post("/api/memory/not-an-id/reject")).status_code == 404


# ---------- /api/repos, /healthz, /metrics ----------
async def test_repos_healthz_and_metrics(api: Stack, counters: Any) -> None:
    async with api.sessions.begin() as session:
        session.add(RepoSetting(repo="acme/calc", allowlisted=True, settings={"base": "main"}))
        session.add(RepoSetting(repo="acme/inventory", allowlisted=False))
    await add_task(api.sessions, "lh-acme-calc-1", repo="acme/calc")
    await add_task(api.sessions, "lh-acme-calc-2", repo="acme/calc")

    repos = await api.get("/api/repos")
    health = await api.get("/healthz")
    metrics = await api.get("/metrics")

    assert repos.status_code == 200
    assert repos.json() == [
        {
            "repo": "acme/calc",
            "allowlisted": True,
            "settings": {"base": "main"},
            "tasks": 2,
            "created_at": repos.json()[0]["created_at"],
        },
        {
            "repo": "acme/inventory",
            "allowlisted": False,
            "settings": {},
            "tasks": 0,
            "created_at": repos.json()[1]["created_at"],
        },
    ]
    assert health.json() == {"status": "ok"}
    assert metrics.status_code == 200
    assert metrics.headers["content-type"].startswith("text/plain")
    # The worker's metrics registry and the API's own counters are one page.
    assert "# TYPE longhaul_active_tasks gauge" in metrics.text
    assert 'longhaul_api_requests_total{method="GET",route="/api/repos",status="200"} 1.0' in (
        metrics.text
    )


async def test_healthz_fails_when_the_database_is_gone() -> None:
    # Nothing listens on port 1, so the check is a connection that fails.
    engine = make_engine("postgresql://nobody:nopass@127.0.0.1:1/never")
    try:
        async with serving(async_sessionmaker[AsyncSession](engine)) as stack:
            response = await stack.get("/healthz")
    finally:
        await engine.dispose()

    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
