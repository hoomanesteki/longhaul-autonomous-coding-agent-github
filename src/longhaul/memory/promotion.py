"""Promotion of lessons when the pull request that carries them is decided (LH-606, ADR-0010).

A task can only propose a lesson, and a merged pull request is one of the three things that can
approve it. The poller calls `Promoter.settle` for each repo on every tick. The rule is short:

* A candidate whose `source_ref` is a pull request number waits while the pull request is open.
  When it is merged the candidate is approved, with `approved_by = merged_pr#<number>`. When it is
  closed without a merge the candidate is rejected. A rejected note stays in the table, out of
  every prompt, and the store keeps it from coming back as a new candidate.
* A candidate whose `source_ref` is a task id belongs to a task that opened no pull request, such
  as a stuck one. It stays a candidate until a person decides in the CLI or the console.

Nothing here keeps a record of what was settled. Both moves go through `promote_candidate` and
`reject_candidate` of the store, which take an item only while it is a candidate. A poll that runs
again, or that overlaps another, finds nothing to move and records nothing. The event follows the
move, so a poll that dies between the two loses an event and never repeats a decision.

The pull request is read with `pull_state`. A pull request that cannot be read (deleted, or the
token lost access) is logged and left alone: not knowing is not a rejection. A GitHub error that
a retry may fix ends the repo's part of the poll, as it does for the rest of the poller.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from longhaul.github.client import GitHubFatal, PullState
from longhaul.memory.store import MemoryEntry

log = logging.getLogger(__name__)

__all__ = [
    "PROMOTED_EVENT",
    "REJECTED_EVENT",
    "EventLog",
    "Promoter",
    "PullReader",
    "Settled",
    "approver_for",
    "decided_by",
]

# The types of the events that the poller records (plan section 3.5).
PROMOTED_EVENT = "memory_promoted"
REJECTED_EVENT = "memory_rejected"


class CandidateStore(Protocol):
    """The part of PgMemoryStore that settling uses."""

    async def list_items(
        self, repo: str, *, status: str | None = None, kind: str | None = None
    ) -> list[MemoryEntry]: ...

    async def promote_candidate(self, item_id: str, by: str) -> bool: ...

    async def reject_candidate(self, item_id: str, by: str) -> bool: ...


class EventLog(Protocol):
    """Where the events go, and how the task of a pull request is found: SqlRecorder."""

    async def record_event(self, task_id: str, type: str, payload: Mapping[str, Any]) -> None: ...

    async def lessons_task(self, repo: str, source_ref: str) -> str | None: ...


class PullReader(Protocol):
    """The part of GitHubClient that settling uses."""

    async def pull_state(self, repo: str, pr_number: int) -> PullState: ...


@dataclass(frozen=True)
class Settled:
    """How many candidates one pass approved and how many it rejected."""

    promoted: int = 0
    rejected: int = 0


def approver_for(pr_number: int) -> str:
    """The `approved_by` of a lesson that a merged pull request approved."""
    return f"merged_pr#{pr_number}"


def decided_by(payload: Mapping[str, Any]) -> str | None:
    """Who settled the lessons of a `memory_promoted` or `memory_rejected` event.

    A rejection names `decided_by`, since nobody approved anything. Before LH-991 it said
    `approved_by`, like a promotion still does, and those rows are in the table for good, so
    a reader asks for both.
    """
    found = payload.get("decided_by", payload.get("approved_by"))
    return found if isinstance(found, str) else None


def pull_number(entry: MemoryEntry) -> int | None:
    """The pull request that a candidate is linked to, or None for a task that opened none."""
    ref = entry.source_ref or ""
    # A task id always starts with "lh-", so digits can only be a pull request number.
    return int(ref) if entry.source == "task" and ref.isascii() and ref.isdigit() else None


class Promoter:
    """Settles the candidates of a repo against the state of their pull requests."""

    def __init__(self, store: CandidateStore, events: EventLog) -> None:
        self._store = store
        self._events = events

    async def settle(self, github: PullReader, repo: str) -> Settled:
        """Approve the candidates of merged pull requests and reject those of closed ones."""
        by_pull: dict[int, list[MemoryEntry]] = {}
        for entry in await self._store.list_items(repo, status="candidate"):
            number = pull_number(entry)
            if number is not None:
                by_pull.setdefault(number, []).append(entry)
        promoted = rejected = 0
        for number, entries in by_pull.items():
            try:
                state = await github.pull_state(repo, number)
            except GitHubFatal as exc:
                log.warning(
                    "cannot read pull request %s#%d, so its lessons wait: %s", repo, number, exc
                )
                continue
            if state == "open":
                continue
            if state == "merged":
                moved = await self._move(entries, approver_for(number), approve=True)
                await self._record(PROMOTED_EVENT, repo, number, moved, approver_for(number))
                promoted += len(moved)
            else:
                by = f"closed_pr#{number}"
                moved = await self._move(entries, by, approve=False)
                await self._record(REJECTED_EVENT, repo, number, moved, by)
                rejected += len(moved)
        return Settled(promoted, rejected)

    async def _move(self, entries: Sequence[MemoryEntry], by: str, *, approve: bool) -> list[str]:
        """The ids of the entries that this call moved. One that moved already is not among them."""
        decide = self._store.promote_candidate if approve else self._store.reject_candidate
        return [entry.id for entry in entries if await decide(entry.id, by)]

    async def _record(
        self, type: str, repo: str, number: int, moved: Sequence[str], by: str
    ) -> None:
        if not moved:
            return
        task_id = await self._events.lessons_task(repo, str(number))
        if task_id is None:
            # The task recorded no lessons event, or its row is gone. The decision still counts,
            # so the event is filed under "<repo>#<number>". That is not a task id: no row of
            # `tasks` has it, `events.task_id` is plain text with no foreign key, and a task's
            # timeline or report never shows the event. The payload has the repo and the pull
            # request, so a query by those finds it.
            task_id = f"{repo}#{number}"
        # A promotion names who approved. A rejection names who decided, as nobody approved.
        key = "approved_by" if type == PROMOTED_EVENT else "decided_by"
        await self._events.record_event(
            task_id, type, {"repo": repo, "pr": number, "items": list(moved), key: by}
        )
        log.info("%s: %d lessons of %s#%d, by %s", type, len(moved), repo, number, by)
