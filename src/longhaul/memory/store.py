"""The memory store on Postgres: what the agent remembers about a repo (LH-603, ADR-0010).

PgMemoryStore implements contracts.MemoryStore on the `memory_items` table. The rules of
ADR-0010 are kept here, at the one place that writes the table:

* A task can only propose. `add_candidate` writes a `candidate` and refuses an item that says
  it is approved, so an unreviewed note cannot reach a prompt by that road.
* Longhaul approves its own findings from the default branch (the repo map and the verified
  commands) with `add_verified`, which accepts nothing else. Everything else is approved by a
  person or a merged pull request through `promote`, which always records who.
* `search` returns approved items unless it is asked for the others (invariant I7), and counts
  each use of what it returns.
* `get_verified` finds the approved item that Longhaul checked at one sha, which is how the repo
  map of a commit is found again without reading the maps of the others. `is_rejected` says
  whether a person turned that item down, which switches the map off for that commit.

The same note is stored once per repo and kind. Case and spacing are folded before the note is
hashed, so "Run pytest -q" and "run  PYTEST -q" are one note. The hash is a column of its own
with a unique index (migration 0008), and an add that finds the note returns the id of the
first one, whatever its status. A rejected note therefore stays rejected when a task proposes
it again.

The contract's MemoryItem has no `source_ref`, so `add_candidate` takes it as an extra keyword.
`MemoryEntry` is what `list_items` returns, with the columns that MemoryItem leaves out.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, get_args

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from longhaul.contracts import Embedder, MemoryItem
from longhaul.db.models import MemoryItemRow

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BRANCH_APPROVER",
    "KINDS",
    "STATUSES",
    "MemoryEntry",
    "MemoryItemNotFoundError",
    "PgMemoryStore",
    "content_hash",
    "normalize_content",
]

# The values of the table's three checks, read from the contract so that the two cannot drift.
KINDS: tuple[str, ...] = get_args(MemoryItem.model_fields["kind"].annotation)
STATUSES: tuple[str, ...] = get_args(MemoryItem.model_fields["status"].annotation)

# `approved_by` of an item that Longhaul approved by running it at a sha of the default branch.
# It names the first row of the table in ADR-0010.
DEFAULT_BRANCH_APPROVER = "default_branch"
# What the default branch can approve: the map of the repo and the commands that run its tests.
_VERIFIABLE_KINDS = ("repo_map", "command")

# Rows that search returns come back one at a time through the index, in order of distance. A
# filter on repo and status is applied after the index scan, so with the default settings a
# query reads the nearest 40 rows of the whole table and drops those of other repos. Iterative
# scan keeps reading until the query has its rows (pgvector 0.8, ADR-0010 "To watch").
# strict_order keeps the order exact, where relaxed_order may return a farther row before a
# nearer one, and a top-k query would then cut at the wrong row.
_ITERATIVE_SCAN = "SET LOCAL hnsw.iterative_scan = strict_order"


class MemoryItemNotFoundError(LookupError):
    """No memory item has the id that was asked for."""


@dataclass(frozen=True)
class MemoryEntry:
    """One row of the table without its vector, as the console and the CLI show it."""

    id: str
    repo: str
    kind: str
    status: str
    source: str
    source_ref: str | None
    approved_by: str | None
    uses: int
    last_used_at: datetime | None
    created_at: datetime
    content: str


def normalize_content(content: str) -> str:
    """Lower case, with every run of whitespace turned into one space."""
    return " ".join(content.lower().split())


def content_hash(content: str) -> str:
    """The key that makes two notes one: sha256 of the normalized text, as hex.

    sha256 and not hash(): the value is stored and must come out the same in every process and
    on every Python version.
    """
    normalized = normalize_content(content)
    if not normalized:
        raise ValueError("a memory item cannot be empty")
    return hashlib.sha256(normalized.encode()).hexdigest()


class PgMemoryStore:
    """MemoryStore on the memory_items table.

    `embedder` makes the vector of each note and of each query. It must be the embedder that
    wrote the rows already in the table, because vectors of two embedders cannot be compared
    (ADR-0010).
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self._sessions = sessions
        self._embedder = embedder

    # ---------- reading ----------
    async def search(
        self, repo: str, query: str, k: int, approved_only: bool = True
    ) -> list[MemoryItem]:
        """The k items of `repo` nearest to `query` by cosine distance, nearest first.

        Only approved items are returned unless `approved_only` is False, which drops the
        filter on status altogether. Each item that comes back has its `uses` counted and its
        `last_used_at` set, in the same transaction as the read.
        """
        if k < 1:
            raise ValueError(f"k must be at least 1, got {k}")
        [vector] = await self._embed([query])
        nearest = select(MemoryItemRow).where(MemoryItemRow.repo == repo)
        if approved_only:
            nearest = nearest.where(MemoryItemRow.status == "approved")
        nearest = nearest.order_by(MemoryItemRow.embedding.cosine_distance(vector)).limit(k)
        async with self._sessions.begin() as session:
            await session.execute(text(_ITERATIVE_SCAN))
            rows = list((await session.scalars(nearest)).all())
            if rows:
                await session.execute(
                    update(MemoryItemRow)
                    .where(MemoryItemRow.id.in_([row.id for row in rows]))
                    # In SQL, so that two searches at once both count.
                    .values(uses=MemoryItemRow.uses + 1, last_used_at=func.now())
                    .execution_options(synchronize_session=False)
                )
        return [_item_of(row) for row in rows]

    async def list_items(
        self, repo: str, *, status: str | None = None, kind: str | None = None
    ) -> list[MemoryEntry]:
        """Every item of `repo`, oldest first, optionally of one status and one kind."""
        # The vector is 384 numbers per row and nothing here shows it.
        stmt = select(MemoryItemRow).options(defer(MemoryItemRow.embedding))
        stmt = stmt.where(MemoryItemRow.repo == repo)
        if status is not None:
            stmt = stmt.where(MemoryItemRow.status == status)
        if kind is not None:
            stmt = stmt.where(MemoryItemRow.kind == kind)
        stmt = stmt.order_by(MemoryItemRow.created_at, MemoryItemRow.id)
        async with self._sessions() as session:
            return [_entry_of(row) for row in await session.scalars(stmt)]

    async def get_verified(self, repo: str, kind: str, source_ref: str) -> MemoryEntry | None:
        """The approved item of the default branch that Longhaul checked at `source_ref`.

        This is how the repo map is found again for a sha (LH-602). A search by meaning would
        find a map for the wrong commit, and listing the items of the repo would read the map of
        every sha it was ever built at. If two items were checked at one sha, as happens when the
        parser changes, the newer one is returned. An item that a person rejected is not.
        """
        stmt = (
            select(MemoryItemRow)
            .options(defer(MemoryItemRow.embedding))
            .where(
                MemoryItemRow.repo == repo,
                MemoryItemRow.kind == kind,
                MemoryItemRow.source_ref == source_ref,
                MemoryItemRow.status == "approved",
                MemoryItemRow.source == "default_branch",
            )
            .order_by(MemoryItemRow.created_at.desc(), MemoryItemRow.id.desc())
            .limit(1)
        )
        async with self._sessions() as session:
            row = (await session.scalars(stmt)).first()
        return None if row is None else _entry_of(row)

    async def is_rejected(self, repo: str, kind: str, source_ref: str) -> bool:
        """Whether a person turned down the default-branch item of `repo` checked at `source_ref`.

        `get_verified` skips a rejected item, so a caller that must not rebuild what a person
        refused asks here when `get_verified` finds nothing (the repo map, LH-602).
        """
        stmt = (
            select(MemoryItemRow.id)
            .where(
                MemoryItemRow.repo == repo,
                MemoryItemRow.kind == kind,
                MemoryItemRow.source_ref == source_ref,
                MemoryItemRow.status == "rejected",
                MemoryItemRow.source == "default_branch",
            )
            .limit(1)
        )
        async with self._sessions() as session:
            return (await session.scalars(stmt)).first() is not None

    # ---------- writing ----------
    async def add_candidate(self, item: MemoryItem, *, source_ref: str | None = None) -> str:
        """Store a proposed note and return its id, or the id of the same note stored before.

        `item.id` is ignored: the database makes the id. A task writes candidates only, so an
        item that says it is approved or rejected is refused.
        """
        if item.status != "candidate":
            raise ValueError(
                f"add_candidate stores candidates only, and this item is {item.status}. "
                "Approval goes through promote (ADR-0010)"
            )
        return await self._add(item, source_ref=source_ref, approved_by=None)

    async def add_verified(self, item: MemoryItem, *, source_ref: str) -> str:
        """Store a map or a command that Longhaul checked at a sha of the default branch.

        `source_ref` is that sha. The item is stored as approved, with the default branch as
        the approver. Nothing but a repo map or a command of the default branch is accepted,
        because that is all ADR-0010 lets Longhaul approve on its own.
        """
        if (
            item.status != "approved"
            or item.source != "default_branch"
            or item.kind not in _VERIFIABLE_KINDS
        ):
            raise ValueError(
                "only an approved repo map or command of the default branch can be verified, "
                f"not a {item.status} {item.kind} from {item.source}"
            )
        return await self._add(item, source_ref=source_ref, approved_by=DEFAULT_BRANCH_APPROVER)

    async def promote(self, item_id: str, by: str) -> None:
        """Approve an item. `by` is recorded as its approver, every time."""
        await self._decide(item_id, by, status="approved", approved_by=by.strip())

    async def reject(self, item_id: str, by: str) -> None:
        """Turn an item down. It stays in the table, out of every prompt.

        The table has no column for who rejected, so `by` goes to the log, and an approver that
        the item had before is cleared: a rejected item has no approver.
        """
        await self._decide(item_id, by, status="rejected", approved_by=None)
        log.info("memory item %s rejected by %s", item_id, by.strip())

    # ---------- the pieces ----------
    async def _embed(self, texts: list[str]) -> list[list[float]]:
        # The local model blocks while it runs, and the event loop must keep its heartbeats.
        return await asyncio.to_thread(self._embedder.embed, texts)

    async def _add(
        self, item: MemoryItem, *, source_ref: str | None, approved_by: str | None
    ) -> str:
        key = {"repo": item.repo, "kind": item.kind, "content_hash": content_hash(item.content)}
        found = await self._id_of(key)
        if found is not None:
            return found  # the note is stored already, and the embedder is not needed
        [vector] = await self._embed([item.content])
        row = {
            **key,
            "content": item.content,
            "embedding": vector,
            "status": item.status,
            "source": item.source,
            "source_ref": source_ref,
            "approved_by": approved_by,
        }
        # Two adds of one note at the same time both get past the lookup above. The index lets
        # one of them in, and the other one reads the id of the winner.
        stmt = (
            insert(MemoryItemRow)
            .values(row)
            .on_conflict_do_nothing(index_elements=list(key))
            .returning(MemoryItemRow.id)
        )
        async with self._sessions.begin() as session:
            inserted = (await session.execute(stmt)).scalar_one_or_none()
        if inserted is not None:
            return str(inserted)
        found = await self._id_of(key)
        if found is None:  # the winner was deleted between the two statements
            raise RuntimeError(f"memory item of {item.repo} vanished while it was being added")
        return found

    async def _id_of(self, key: dict[str, Any]) -> str | None:
        stmt = select(MemoryItemRow.id).where(
            *(getattr(MemoryItemRow, name) == value for name, value in key.items())
        )
        async with self._sessions() as session:
            found = (await session.execute(stmt)).scalar_one_or_none()
        return None if found is None else str(found)

    async def _decide(self, item_id: str, by: str, *, status: str, approved_by: str | None) -> None:
        if not by.strip():
            raise ValueError("who decided? the name must not be blank")
        stmt = (
            update(MemoryItemRow)
            .where(MemoryItemRow.id == _parse_id(item_id))
            .values(status=status, approved_by=approved_by)
            .returning(MemoryItemRow.id)
        )
        async with self._sessions.begin() as session:
            if (await session.execute(stmt)).first() is None:
                raise MemoryItemNotFoundError(f"no memory item {item_id!r}")


def _parse_id(item_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(item_id)
    except ValueError:
        raise MemoryItemNotFoundError(f"no memory item {item_id!r}") from None


def _entry_of(row: MemoryItemRow) -> MemoryEntry:
    return MemoryEntry(
        id=str(row.id),
        repo=row.repo,
        kind=row.kind,
        status=row.status,
        source=row.source,
        source_ref=row.source_ref,
        approved_by=row.approved_by,
        uses=row.uses,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
        content=row.content,
    )


def _item_of(row: MemoryItemRow) -> MemoryItem:
    return MemoryItem.model_validate(
        {
            "id": str(row.id),
            "repo": row.repo,
            "kind": row.kind,
            "content": row.content,
            "status": row.status,
            "source": row.source,
        }
    )
