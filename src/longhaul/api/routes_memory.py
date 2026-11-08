"""The memory routes: look at the notes the agent keeps, and decide on them (LH-801).

These are the console's version of `longhaul memory list|approve|reject`, and they go through the
same store, so ADR-0010 is kept in one place. Approving writes `approved_by = "console"`, which
tells a reader of the table that a person decided in the console. A person can approve an item
that was rejected, and reject an item that was approved: the store allows both, as the CLI does.

The text of a note came from a model reading a transcript, so it is redacted on the way out.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query

from longhaul.api.deps import ApiContext, Ctx
from longhaul.api.errors import not_found
from longhaul.api.redaction import redact
from longhaul.api.schemas import MemoryItemView
from longhaul.memory.store import MemoryEntry, MemoryItemNotFoundError

router = APIRouter(prefix="/api/memory")

BY = "console"
DEFAULT_LIMIT = 200
MAX_LIMIT = 1_000

Status = Literal["candidate", "approved", "rejected"]
Kind = Literal["repo_map", "command", "convention", "pitfall", "lesson"]


def view(entry: MemoryEntry) -> MemoryItemView:
    return MemoryItemView.model_validate(
        {
            "id": entry.id,
            "repo": entry.repo,
            "kind": entry.kind,
            "status": entry.status,
            "source": entry.source,
            "source_ref": entry.source_ref,
            "approved_by": entry.approved_by,
            "uses": entry.uses,
            "last_used_at": entry.last_used_at,
            "created_at": entry.created_at,
            "content": redact(entry.content),
        }
    )


@router.get("")
async def list_memory(
    ctx: Ctx,
    repo: Annotated[str | None, Query(max_length=200)] = None,
    status: Status | None = None,
    kind: Kind | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> list[MemoryItemView]:
    """Memory items, oldest first. With no `repo`, the items of every repo."""
    entries = await ctx.memory.list_items(repo, status=status, kind=kind, limit=limit)
    return [view(entry) for entry in entries]


@router.post("/{item_id}/approve")
async def approve(item_id: str, ctx: Ctx) -> MemoryItemView:
    """Approve an item, so that it can reach the prompt of later tasks."""
    return await decide(ctx, item_id, approve=True)


@router.post("/{item_id}/reject")
async def reject(item_id: str, ctx: Ctx) -> MemoryItemView:
    """Reject an item. It stays in the table and never reaches a prompt."""
    return await decide(ctx, item_id, approve=False)


async def decide(ctx: ApiContext, item_id: str, *, approve: bool) -> MemoryItemView:
    try:
        if approve:
            await ctx.memory.promote(item_id, BY)
        else:
            await ctx.memory.reject(item_id, BY)
        return view(await ctx.memory.get_item(item_id))
    except MemoryItemNotFoundError:
        raise not_found("memory item") from None
    except ValueError as exc:  # a blank name; BY is not blank, so this is a store rule
        raise HTTPException(status_code=422, detail=str(exc)) from None
