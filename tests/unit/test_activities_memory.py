"""The load_memory activity: the memory block of a task, written once for the whole task (LH-604).

The workflow calls it once after prepare_workspace and keeps the ref. It reads the repo map that
prepare_workspace wrote, asks the store for the commands and the lessons, renders the block
(longhaul.memory.retrieve) and stores it as a blob that every model turn reads. These tests give
it a store in a list and the memory blob store, so they need no database.
"""

import pytest
from longhaul.durable.activities.memory import MemoryActivities
from longhaul.memory.retrieve import HEADER
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from tests.fakes.memory import REPO, FakeMemory, row

from longhaul.blobs.base import blob_key
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import BlobRef, RepoSettings, TaskSpec
from longhaul.durable import acts
from longhaul.durable.payload_guard import DEFAULT_LIMIT

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

TASK = "lh-acme-inventory-7"
MAP = "Python files of this repo, most relevant first (1 of 1 shown):\ninventory/stock.py"


def make_spec(
    title: str = "remove() ignores a missing sku", body: str = "It should raise."
) -> TaskSpec:
    return TaskSpec(
        task_id=TASK,
        repo=REPO,
        issue_number=7,
        title=title,
        body=body,
        settings=RepoSettings(repo=REPO),
    )


async def load(
    memory: FakeMemory | None,
    blobs: MemoryBlobStore,
    spec: TaskSpec | None = None,
    repo_map_ref: BlobRef | None = None,
) -> BlobRef:
    activities = MemoryActivities(memory, blobs)
    ref: BlobRef = await ActivityEnvironment().run(
        activities.load_memory,
        acts.LoadMemoryInput(spec=spec or make_spec(), repo_map_ref=repo_map_ref),
    )
    return ref


async def text_of(blobs: MemoryBlobStore, ref: BlobRef) -> str:
    return (await blobs.get(ref)).decode()


async def test_the_block_is_written_as_a_blob_of_the_task() -> None:
    blobs = MemoryBlobStore()
    map_ref = await blobs.put(MAP.encode(), key=blob_key(TASK, "memory", 0, "repo-map.txt"))
    memory = FakeMemory(
        [
            row("Raise InventoryError for a missing sku.", kind="lesson"),
            row("pytest -q", kind="command", source="default_branch"),
        ]
    )

    ref = await load(memory, blobs, repo_map_ref=map_ref)

    assert ref.key == blob_key(TASK, "memory", 0, "memory-block.txt")
    block = await text_of(blobs, ref)
    assert block.startswith(HEADER)
    for part in ("inventory/stock.py", "- pytest -q", "- [lesson] Raise InventoryError"):
        assert part in block


async def test_the_lessons_are_found_by_the_title_and_the_body_of_the_issue() -> None:
    memory = FakeMemory([row("a lesson")])

    await load(memory, MemoryBlobStore(), make_spec("Title words", "Body words"))

    (asked,) = memory.searches
    assert asked["query"] == "Title words\n\nBody words"
    assert asked["repo"] == REPO


async def test_a_worker_without_a_store_writes_an_empty_block() -> None:
    # The workflow calls this only for a worker that keeps memory. If a task lands on one that
    # does not, the prompt gets no block and the task goes on.
    blobs = MemoryBlobStore()

    ref = await load(None, blobs)

    assert await text_of(blobs, ref) == ""


async def test_a_repo_with_nothing_approved_and_no_map_writes_an_empty_block() -> None:
    blobs = MemoryBlobStore()

    ref = await load(FakeMemory([row("PROPOSED", status="candidate")]), blobs)

    assert await text_of(blobs, ref) == ""


async def test_a_map_that_is_gone_from_the_blob_store_is_left_out() -> None:
    # The map is a hint. A task that lost it still gets the commands and the lessons.
    blobs = MemoryBlobStore()
    gone = BlobRef(key=blob_key(TASK, "memory", 0, "repo-map.txt"), sha256="0" * 64, size=3)
    memory = FakeMemory([row("pytest -q", kind="command")])

    ref = await load(memory, blobs, repo_map_ref=gone)

    block = await text_of(blobs, ref)
    assert "- pytest -q" in block
    assert "inventory/stock.py" not in block


async def test_a_store_that_fails_is_retried_by_temporal_and_not_hidden() -> None:
    class Down(FakeMemory):
        async def search(self, *args: object, **kwargs: object) -> list:  # type: ignore[override]
            raise ConnectionError("database is down")

    with pytest.raises(ConnectionError, match="database is down"):
        await load(Down([]), MemoryBlobStore())


async def test_a_retried_attempt_writes_the_same_key() -> None:
    blobs = MemoryBlobStore()
    memory = FakeMemory([row("a lesson about remove", kind="lesson")])

    first = await load(memory, blobs)
    second = await load(memory, blobs)

    assert first == second


async def test_an_oversized_spec_is_refused_for_good() -> None:
    spec = make_spec(body="x" * (DEFAULT_LIMIT + 1))

    with pytest.raises(ApplicationError) as caught:
        await load(FakeMemory([]), MemoryBlobStore(), spec)

    assert caught.value.non_retryable


def test_the_activity_list_is_what_a_worker_registers() -> None:
    activities = MemoryActivities(None, MemoryBlobStore()).activities
    assert [a.__name__ for a in activities] == ["load_memory"]
