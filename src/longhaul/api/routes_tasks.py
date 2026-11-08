"""The task routes: list, detail, timeline, turns, tool outputs and the five actions (LH-801).

Reads come from the database and the blob store. Anything that has to be current, the phase of a
running task, comes from a status query to its workflow, and anything that changes a task is a
signal, sent through durable/client.py with `by="console"`. The API never starts a workflow and
never touches the workflow's state in any other way, so a task behaves the same whether a person
steered it from the console, from an issue comment or from the CLI.

A turn or an output is read from the blob store and redacted on the way out (redaction.py). The
tool output is redacted whole before it is cut into a page, so a secret cannot be split by the
edge of a page, and the line numbers of a page are those of the redacted text.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import TypeAdapter
from temporalio.client import Client, WorkflowQueryFailedError

from longhaul.api import paging, queries
from longhaul.api.deps import ApiContext, Ctx, authorize
from longhaul.api.errors import (
    PROTECTED,
    STORE_ERRORS,
    TEMPORAL_ERRORS,
    not_found,
    unreadable,
    with_client,
)
from longhaul.api.redaction import redact, redact_json
from longhaul.api.schemas import (
    ActionResult,
    BudgetRequest,
    CancelRequest,
    HintRequest,
    LiveState,
    RejectPlanRequest,
    Signal,
    TaskDetail,
    TaskPage,
    TimelinePage,
    ToolOutput,
    TurnDetail,
)
from longhaul.blobs.base import BlobNotFoundError
from longhaul.contracts import LLMResponse, Message, TaskStatus
from longhaul.durable import acts
from longhaul.durable import client as durable

router = APIRouter(
    prefix="/api/tasks",
    dependencies=[Depends(authorize)],
    responses=PROTECTED,
)

# The name every signal of this API is sent under. The workflow keeps it: a hint is shown to the
# model as "console: <text>" and a cancel says "cancelled by console".
BY = acts.CONSOLE_NAME

DEFAULT_PAGE = 50
MAX_PAGE = 200
DEFAULT_TIMELINE_PAGE = 100
MAX_TIMELINE_PAGE = 500


# ---------- the list and the detail ----------
@router.get("")
async def list_tasks(
    ctx: Ctx,
    status: Annotated[str | None, Query(max_length=50)] = None,
    repo: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    cursor: str | None = None,
) -> TaskPage:
    """Tasks, newest first. `status` and `repo` narrow the list."""
    try:
        return await queries.list_tasks(
            ctx.sessions, status=status, repo=repo, limit=limit, cursor=cursor
        )
    except paging.CursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/{task_id}")
async def get_task(task_id: str, ctx: Ctx) -> TaskDetail:
    """The row of a task, the answer of its workflow if there is one, and its plan."""
    task = await queries.find_task(ctx.sessions, task_id)
    if task is None:
        raise not_found("task")
    state, live = await live_status(ctx, task_id)
    return TaskDetail(
        task=task, workflow=state, live=live, plan=await queries.latest_plan(ctx.sessions, task_id)
    )


async def live_status(ctx: ApiContext, task_id: str) -> tuple[LiveState, TaskStatus | None]:
    """Where the workflow stands, and its status when a worker answers the query.

    A page must load when Temporal is down, so every failure here is a value and not an error:
    "unreachable" for a server that does not answer, and no status for a workflow that is gone
    or has no worker to ask.
    """
    try:
        client = await ctx.temporal()
        state = await durable.workflow_state(client, task_id)
    except TEMPORAL_ERRORS:
        return "unreachable", None
    if state == "not_found":
        return state, None
    handle = durable.get_task(client, task_id)
    try:
        return state, await durable.read_status(handle, timeout_s=ctx.status_timeout_s)
    except (TimeoutError, *TEMPORAL_ERRORS, WorkflowQueryFailedError):
        return state, None


# ---------- the timeline ----------
@router.get("/{task_id}/timeline")
async def get_timeline(
    task_id: str,
    ctx: Ctx,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_TIMELINE_PAGE)] = DEFAULT_TIMELINE_PAGE,
) -> TimelinePage:
    """Events, model calls and tool calls of a task, merged by time, oldest first."""
    try:
        page = await queries.timeline(ctx.sessions, task_id, limit=limit, cursor=cursor)
    except paging.CursorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    # A task with nothing recorded yet is an empty page. A task the database never heard of is not.
    if not page.items and cursor is None and not await queries.task_exists(ctx.sessions, task_id):
        raise not_found("task")
    return page


# ---------- a turn ----------
@router.get("/{task_id}/turns/{turn_index}")
async def get_turn(
    task_id: str,
    turn_index: int,
    ctx: Ctx,
    attempt: Annotated[int | None, Query(ge=1)] = None,
) -> TurnDetail:
    """What was sent to the model on one turn and what came back, with secrets redacted.

    A turn that was asked again has several attempts. The last is shown unless `attempt` names
    another.
    """
    rows = await queries.find_turns(ctx.sessions, task_id, turn_index)
    if not rows:
        raise not_found("turn")
    attempts = [row.attempt for row in rows]
    chosen = rows[-1] if attempt is None else next((r for r in rows if r.attempt == attempt), None)
    if chosen is None:
        raise not_found("attempt of this turn")
    request = await read_blob(ctx, chosen.request_ref, TypeAdapter(list[Message]), "the request")
    response = await read_blob(ctx, chosen.response_ref, TypeAdapter(LLMResponse), "the reply")
    return TurnDetail(
        turn_index=chosen.turn_index,
        attempt=chosen.attempt,
        attempts=attempts,
        role=chosen.role,
        started_at=chosen.started_at,
        ended_at=chosen.ended_at,
        input_tokens=chosen.input_tokens,
        output_tokens=chosen.output_tokens,
        cache_read_tokens=chosen.cache_read_tokens,
        cache_write_tokens=chosen.cache_write_tokens,
        cost_usd=float(chosen.cost_usd),
        stop_reason=chosen.stop_reason,
        latency_ms=chosen.latency_ms,
        request=request,
        response=response,
    )


async def read_blob[T](
    ctx: ApiContext, key: str | None, adapter: TypeAdapter[T], what: str
) -> T | None:
    """A stored JSON blob, redacted and parsed. None when the turn has none (or it is gone)."""
    if key is None:
        return None
    try:
        raw = json.loads(await ctx.blobs.get_by_key(key))
        return adapter.validate_python(redact_json(raw))
    except BlobNotFoundError:
        return None
    except STORE_ERRORS as exc:
        raise unreadable(what, exc) from None


# ---------- a tool output ----------
@router.get("/{task_id}/tool-calls/{call_id}/output")
async def get_tool_output(
    task_id: str,
    call_id: str,
    ctx: Ctx,
    start: Annotated[int, Query(ge=1)] = 1,
    end: Annotated[int | None, Query(ge=1)] = None,
) -> ToolOutput:
    """Lines `start` to `end` (1-based, inclusive) of the full output of a tool call."""
    call = await queries.find_tool_call(ctx.sessions, task_id, call_id)
    if call is None:
        raise not_found("tool call")
    if call.output_ref is None:
        raise not_found("stored output for this tool call")
    try:
        text = (await ctx.blobs.get_by_key(call.output_ref)).decode("utf-8", errors="replace")
    except BlobNotFoundError:
        raise not_found("stored output for this tool call") from None
    except STORE_ERRORS as exc:
        raise unreadable("the output", exc) from None
    try:
        page = paging.slice_lines(redact(text), start, end)
    except paging.RangeError as exc:
        # A start past the end is a range the output does not have. An end before the start is
        # the caller's mistake.
        code = 416 if isinstance(exc, paging.BeyondEndError) else 422
        raise HTTPException(status_code=code, detail=str(exc)) from None
    return ToolOutput(
        call_id=call.id,
        name=call.name,
        ok=call.ok,
        exit_code=call.exit_code,
        text=page.text,
        start_line=page.start_line,
        end_line=page.end_line,
        total_lines=page.total_lines,
        next_start=page.next_start,
    )


# ---------- actions ----------
def sent(task_id: str, signal: Signal) -> ActionResult:
    return ActionResult(task_id=task_id, signal=signal, by=BY)


@router.post("/{task_id}/approve-plan")
async def approve_plan(task_id: str, ctx: Ctx) -> ActionResult:
    """Approve the plan the task is waiting on."""
    await with_client(ctx.temporal, lambda c: durable.approve_plan(c, task_id, by=BY))
    return sent(task_id, "approve_plan")


@router.post("/{task_id}/reject-plan")
async def reject_plan(task_id: str, body: RejectPlanRequest, ctx: Ctx) -> ActionResult:
    """Reject the plan with feedback. The planner writes a new version."""
    await with_client(
        ctx.temporal, lambda c: durable.reject_plan(c, task_id, by=BY, feedback=body.feedback)
    )
    return sent(task_id, "reject_plan")


@router.post("/{task_id}/hint")
async def hint(task_id: str, body: HintRequest, ctx: Ctx) -> ActionResult:
    """Give the agent a hint. A paused task resumes with it."""
    await with_client(ctx.temporal, lambda c: durable.send_hint(c, task_id, by=BY, text=body.text))
    return sent(task_id, "hint")


@router.post("/{task_id}/budget")
async def budget(task_id: str, body: BudgetRequest, ctx: Ctx) -> ActionResult:
    """Add dollars, turns or both to the budget of the task."""

    async def job(client: Client) -> None:
        await durable.add_budget(client, task_id, by=BY, usd=body.usd, turns=body.turns)

    await with_client(ctx.temporal, job)
    return sent(task_id, "add_budget")


@router.post("/{task_id}/cancel")
async def cancel(task_id: str, ctx: Ctx, body: CancelRequest | None = None) -> ActionResult:
    """Stop the task. It cleans up and ends with the cancelled outcome."""
    reason = (body or CancelRequest()).reason
    await with_client(ctx.temporal, lambda c: durable.cancel_task(c, task_id, by=BY, reason=reason))
    return sent(task_id, "cancel")
