"""The in-memory task store, which the poller tests use in place of Postgres (LH-307).

tests/integration/test_poller.py runs the same cases against SqlTaskStore, so the two cannot
drift apart without a test noticing.
"""

import pytest

from longhaul.contracts import Phase, RepoSettings, TaskSpec
from longhaul.db.tasks import ACTIVE, MemoryTaskStore

pytestmark = [pytest.mark.unit, pytest.mark.phase3]


def spec(
    number: int = 1, title: str = "add() is wrong", test_command: str = "pytest -q"
) -> TaskSpec:
    return TaskSpec(
        task_id=f"lh-acme-calc-{number}",
        repo="acme/calc",
        issue_number=number,
        title=title,
        body="",
        settings=RepoSettings(repo="acme/calc", test_command=test_command),
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
