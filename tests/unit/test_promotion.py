"""The rules of memory promotion with fakes in place of Postgres and GitHub (LH-606).

tests/integration/test_memory_promotion.py runs the same rules on a real database.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from longhaul.db.records import LESSONS_EVENT
from longhaul.durable import acts
from longhaul.github.client import GitHubFatal, GitHubRetryable, PullState
from longhaul.memory.promotion import (
    PROMOTED_EVENT,
    REJECTED_EVENT,
    Promoter,
    approver_for,
    pull_number,
)
from longhaul.memory.store import MemoryEntry

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

REPO = "acme/inventory"


def entry(item_id: str, ref: str | None, source: str = "task") -> MemoryEntry:
    return MemoryEntry(
        id=item_id,
        repo=REPO,
        kind="lesson",
        status="candidate",
        source=source,
        source_ref=ref,
        approved_by=None,
        uses=0,
        last_used_at=None,
        created_at=datetime(2026, 11, 3, tzinfo=UTC),
        content=f"note {item_id}",
    )


class FakeStore:
    def __init__(self, entries: list[MemoryEntry]) -> None:
        self.entries = entries
        self.decided: list[tuple[str, str, str]] = []
        self.taken: set[str] = set()  # items that something else moved first

    async def list_items(
        self, repo: str, *, status: str | None = None, kind: str | None = None
    ) -> list[MemoryEntry]:
        assert (repo, status, kind) == (REPO, "candidate", None)
        return self.entries

    async def promote_candidate(self, item_id: str, by: str) -> bool:
        self.decided.append(("promote", item_id, by))
        return item_id not in self.taken

    async def reject_candidate(self, item_id: str, by: str) -> bool:
        self.decided.append(("reject", item_id, by))
        return item_id not in self.taken


class FakeEvents:
    def __init__(self, tasks: dict[str, str] | None = None) -> None:
        self.tasks = tasks or {}
        self.recorded: list[tuple[str, str, dict[str, Any]]] = []

    async def record_event(self, task_id: str, type: str, payload: Mapping[str, Any]) -> None:
        self.recorded.append((task_id, type, dict(payload)))

    async def lessons_task(self, repo: str, source_ref: str) -> str | None:
        assert repo == REPO
        return self.tasks.get(source_ref)


class FakePulls:
    def __init__(self, states: dict[int, PullState | Exception]) -> None:
        self.states = states
        self.asked: list[int] = []

    async def pull_state(self, repo: str, pr_number: int) -> PullState:
        self.asked.append(pr_number)
        state = self.states[pr_number]
        if isinstance(state, Exception):
            raise state
        return state


def test_the_event_names_match_the_ones_the_workflow_records() -> None:
    assert LESSONS_EVENT == acts.LESSONS_EVENT


def test_only_a_task_candidate_with_a_number_is_linked_to_a_pull_request() -> None:
    assert pull_number(entry("a", "17")) == 17
    assert pull_number(entry("a", "lh-acme-inventory-7")) is None
    assert pull_number(entry("a", None)) is None
    assert pull_number(entry("a", "17", source="human")) is None
    assert pull_number(entry("a", "١٧")) is None  # digits of another script


async def test_each_pull_request_is_asked_about_once_and_open_ones_are_left() -> None:
    store = FakeStore([entry("a", "17"), entry("b", "17"), entry("c", "18")])
    pulls = FakePulls({17: "merged", 18: "open"})
    events = FakeEvents({"17": "lh-acme-inventory-7"})

    settled = await Promoter(store, events).settle(pulls, REPO)

    assert (settled.promoted, settled.rejected) == (2, 0)
    assert pulls.asked == [17, 18]
    assert store.decided == [
        ("promote", "a", approver_for(17)),
        ("promote", "b", approver_for(17)),
    ]
    assert events.recorded == [
        (
            "lh-acme-inventory-7",
            PROMOTED_EVENT,
            {"repo": REPO, "pr": 17, "items": ["a", "b"], "approved_by": "merged_pr#17"},
        )
    ]


async def test_a_task_without_a_pull_request_is_never_decided_here() -> None:
    store = FakeStore([entry("a", "lh-acme-inventory-9")])
    pulls = FakePulls({})

    settled = await Promoter(store, FakeEvents()).settle(pulls, REPO)

    assert (settled.promoted, settled.rejected) == (0, 0)
    assert pulls.asked == []
    assert store.decided == []


async def test_an_item_that_moved_already_is_not_counted_or_recorded() -> None:
    store = FakeStore([entry("a", "17")])
    store.taken.add("a")
    events = FakeEvents()

    settled = await Promoter(store, events).settle(FakePulls({17: "closed"}), REPO)

    assert (settled.promoted, settled.rejected) == (0, 0)
    assert events.recorded == []


async def test_an_event_without_a_known_task_is_filed_under_the_pull_request() -> None:
    events = FakeEvents()

    await Promoter(FakeStore([entry("a", "17")]), events).settle(FakePulls({17: "closed"}), REPO)

    assert [(task, type) for task, type, _ in events.recorded] == [(f"{REPO}#17", REJECTED_EVENT)]


async def test_a_pull_request_that_cannot_be_read_waits_and_the_others_go_on() -> None:
    store = FakeStore([entry("a", "17"), entry("b", "18")])
    pulls = FakePulls({17: GitHubFatal("404 Not Found"), 18: "merged"})

    settled = await Promoter(store, FakeEvents()).settle(pulls, REPO)

    assert (settled.promoted, settled.rejected) == (1, 0)
    assert [item for _, item, _ in store.decided] == ["b"]


async def test_an_error_that_a_retry_may_fix_stops_the_pass() -> None:
    pulls = FakePulls({17: GitHubRetryable("502")})

    with pytest.raises(GitHubRetryable):
        await Promoter(FakeStore([entry("a", "17")]), FakeEvents()).settle(pulls, REPO)
