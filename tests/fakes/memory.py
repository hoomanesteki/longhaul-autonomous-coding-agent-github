"""A memory store in a list, for tests of retrieval that need no database (LH-604).

FakeMemory answers the two reads that `render_memory_block` makes, with the semantics of
PgMemoryStore: `search` returns the items of one repo nearest to the query first, only approved
ones unless it is asked for the rest, and `list_items` lists a repo's items oldest first. The
distance is the real one, a dot product of HashEmbedder vectors, so a test can say which note is
nearest without a database. The calls are kept, so a test can check what was asked.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from longhaul.contracts import MemoryItem
from longhaul.memory.embedder import HashEmbedder

REPO = "acme/inventory"
_ids = itertools.count(1)


@dataclass(frozen=True)
class Row:
    """One stored item. `content` and `status` are the fields that `list_items` entries have."""

    id: str
    repo: str
    kind: str
    content: str
    status: str
    source: str


def row(
    content: str,
    *,
    kind: str = "lesson",
    status: str = "approved",
    source: str = "merged_pr",
    repo: str = REPO,
    id: str | None = None,
) -> Row:
    return Row(id or f"item-{next(_ids)}", repo, kind, content, status, source)


class FakeMemory:
    """The reads of PgMemoryStore on a list of rows.

    `leaky` makes `search` and `list_items` ignore the status filter, as a store with a bug
    would. A caller that trusts the store alone then shows the leak, which is what the tests of
    invariant I7 use it for.
    """

    def __init__(self, rows: Iterable[Row] = (), *, leaky: bool = False) -> None:
        self.rows = list(rows)
        self.leaky = leaky
        self.searches: list[dict[str, Any]] = []
        self.listings: list[dict[str, Any]] = []
        self._embedder = HashEmbedder()

    async def search(
        self,
        repo: str,
        query: str,
        k: int,
        approved_only: bool = True,
        *,
        kinds: Sequence[str] | None = None,
    ) -> list[MemoryItem]:
        self.searches.append({"repo": repo, "query": query, "k": k, "approved_only": approved_only})
        found = [r for r in self.rows if r.repo == repo]
        if approved_only and not self.leaky:
            found = [r for r in found if r.status == "approved"]
        if kinds is not None:
            found = [r for r in found if r.kind in kinds]
        [wanted] = self._embedder.embed([query])
        vectors = self._embedder.embed([r.content for r in found])
        scored = [
            (sum(a * b for a, b in zip(wanted, vector, strict=True)), index)
            for index, vector in enumerate(vectors)
        ]
        # The nearest first; equal scores keep the order the rows were added in.
        order = [index for _, index in sorted(scored, key=lambda pair: (-pair[0], pair[1]))]
        return [_item(found[index]) for index in order[:k]]

    async def list_items(
        self, repo: str, *, status: str | None = None, kind: str | None = None
    ) -> list[Row]:
        self.listings.append({"repo": repo, "status": status, "kind": kind})
        found = [r for r in self.rows if r.repo == repo]
        if status is not None and not self.leaky:
            found = [r for r in found if r.status == status]
        if kind is not None:
            found = [r for r in found if r.kind == kind]
        return found


def _item(r: Row) -> MemoryItem:
    return MemoryItem.model_validate(
        {
            "id": r.id,
            "repo": r.repo,
            "kind": r.kind,
            "content": r.content,
            "status": r.status,
            "source": r.source,
        }
    )
