"""The in-memory task store, which the poller tests use in place of Postgres (LH-307).

tests/integration/test_poller.py runs the same cases against SqlTaskStore, so the two cannot
drift apart without a test noticing.
"""

from datetime import UTC, datetime, timedelta

import pytest

from longhaul.contracts import Phase, RepoSettings, TaskSpec
from longhaul.db.tasks import ACTIVE, CommentCursor, MemoryTaskStore

pytestmark = [pytest.mark.unit, pytest.mark.phase3]


NOON = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def spec(
    number: int = 1,
    title: str = "add() is wrong",
    test_command: str = "pytest -q",
    *,
    task_id: str | None = None,
    repo: str = "acme/calc",
) -> TaskSpec:
    return TaskSpec(
        task_id=task_id or f"lh-acme-calc-{number}",
        repo=repo,
        issue_number=number,
        title=title,
        body="",
        settings=RepoSettings(repo=repo, test_command=test_command),
    )


async def test_a_pickup_writes_an_active_queued_row_with_the_settings_as_json() -> None:
    store = MemoryTaskStore()

    await store.record_pickup(spec())

    row = store.rows["lh-acme-calc-1"]
    assert (row.repo, row.issue_number, row.title) == ("acme/calc", 1, "add() is wrong")
    assert (row.status, row.phase) == (ACTIVE, Phase.QUEUED.value)
    assert row.settings["test_command"] == "pytest -q"
    assert row.started_at.tzinfo is not None


async def test_a_second_pickup_updates_the_title_and_settings_and_keeps_the_rest() -> None:
    store = MemoryTaskStore()
    await store.record_pickup(spec())
    row = store.rows["lh-acme-calc-1"]
    row.status, row.phase = "done", "done"
    started = row.started_at

    await store.record_pickup(spec(title="the title changed", test_command="pytest -x"))

    assert list(store.rows) == ["lh-acme-calc-1"]
    assert (row.status, row.phase, row.started_at) == ("done", "done", started)
    assert row.title == "the title changed"
    assert row.settings["test_command"] == "pytest -x"


async def test_known_answers_with_the_ids_that_have_a_row() -> None:
    store = MemoryTaskStore()
    await store.record_pickup(spec(1))
    await store.record_pickup(spec(3))

    assert await store.known(["lh-acme-calc-1", "lh-acme-calc-2", "lh-acme-calc-3"]) == {
        "lh-acme-calc-1",
        "lh-acme-calc-3",
    }
    assert await store.known([]) == set()


# --- the comment cursor (LH-308) ---------------------------------------------------------------


async def test_a_pickup_without_a_cursor_leaves_the_cursor_empty() -> None:
    store = MemoryTaskStore()

    await store.record_pickup(spec())

    row = store.rows["lh-acme-calc-1"]
    assert (row.last_comment_at, row.last_comment_id) == (None, None)
    assert row.cursor is None


async def test_a_pickup_stores_the_cursor_it_is_given() -> None:
    store = MemoryTaskStore()

    await store.record_pickup(spec(), cursor=CommentCursor(NOON, 77))

    row = store.rows["lh-acme-calc-1"]
    assert (row.last_comment_at, row.last_comment_id) == (NOON, 77)
    assert row.cursor == CommentCursor(NOON, 77)


async def test_a_second_pickup_does_not_move_the_cursor() -> None:
    store = MemoryTaskStore()
    await store.record_pickup(spec(), cursor=CommentCursor(NOON, 77))

    await store.record_pickup(spec(title="the title changed"), cursor=CommentCursor(NOON, 5))

    assert store.rows["lh-acme-calc-1"].cursor == CommentCursor(NOON, 77)


async def test_the_cursor_only_moves_forward() -> None:
    store = MemoryTaskStore()
    await store.record_pickup(spec(), cursor=CommentCursor(NOON, 10))
    later = CommentCursor(NOON + timedelta(minutes=5), 30)

    await store.advance_comments("lh-acme-calc-1", later)
    await store.advance_comments("lh-acme-calc-1", CommentCursor(NOON + timedelta(minutes=1), 20))

    assert store.rows["lh-acme-calc-1"].cursor == later


async def test_the_cursor_can_start_from_nothing() -> None:
    store = MemoryTaskStore()
    await store.record_pickup(spec())

    await store.advance_comments("lh-acme-calc-1", CommentCursor(NOON, 3))

    assert store.rows["lh-acme-calc-1"].cursor == CommentCursor(NOON, 3)


async def test_advancing_a_task_that_has_no_row_is_not_an_error() -> None:
    store = MemoryTaskStore()

    await store.advance_comments("lh-acme-calc-9", CommentCursor(NOON, 3))

    assert store.rows == {}


async def test_the_latest_attempt_of_each_issue_is_the_one_with_the_highest_number() -> None:
    store = MemoryTaskStore()
    for task_id, number in [
        ("lh-acme-calc-1", 1),
        ("lh-acme-calc-1-a2", 1),
        ("lh-acme-calc-1-a10", 1),
        ("lh-acme-calc-1-a9", 1),
        ("lh-acme-calc-2", 2),
        ("lh-acme-calc-3", 3),
    ]:
        await store.record_pickup(spec(number, task_id=task_id))

    found = await store.latest_attempts("acme/calc", [1, 2, 4])

    assert {number: row.id for number, row in found.items()} == {
        1: "lh-acme-calc-1-a10",
        2: "lh-acme-calc-2",
    }


async def test_the_latest_attempt_is_looked_up_in_one_repo_only() -> None:
    store = MemoryTaskStore()
    await store.record_pickup(spec(1))
    await store.record_pickup(spec(1, task_id="lh-acme-text-1", repo="acme/text"))

    found = await store.latest_attempts("acme/text", [1])

    assert [row.id for row in found.values()] == ["lh-acme-text-1"]
    assert await store.latest_attempts("acme/calc", []) == {}
