"""The janitor: bare repos of tasks that ended more than seven days ago are deleted (LH-507).

A task keeps its bare repo after it ends, so a person can read the checkpoints when they look at
a stuck or failed task. The poller schedule runs the janitor, which removes a repo once seven days
have passed since the outcome of its task. A repo whose task has no outcome is the repo of a task
that is still running, and the janitor never touches it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from longhaul.durable.activities.janitor import JanitorActivities
from temporalio.testing import ActivityEnvironment

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.checkpoints.git_store import GitCheckpointStore
from longhaul.db.records import OUTCOME_EVENT, MemoryRecorder
from longhaul.durable import acts

pytestmark = [pytest.mark.unit, pytest.mark.phase5]

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


class Repos:
    """The repos of a worker, by task id. Removing one takes it from the list."""

    def __init__(self, *task_ids: str) -> None:
        self.ids = list(task_ids)
        self.removed: list[str] = []
        self.broken: set[str] = set()

    async def task_ids(self) -> list[str]:
        return list(self.ids)

    async def cleanup_task(self, task_id: str) -> None:
        if task_id in self.broken:
            raise OSError(f"cannot remove {task_id}")
        self.ids.remove(task_id)
        self.removed.append(task_id)


def janitor(repos: Repos, recorder: MemoryRecorder) -> JanitorActivities:
    return JanitorActivities(repos, recorder, clock=lambda: NOW)


async def sweep(runner: JanitorActivities) -> acts.JanitorResult:
    result: acts.JanitorResult = await ActivityEnvironment().run(runner.clean_repos)
    return result


def ended(recorder: MemoryRecorder, task_id: str, ago: timedelta) -> None:
    recorder.ended[task_id] = NOW - ago


async def test_the_repo_of_a_task_that_ended_eight_days_ago_is_removed() -> None:
    repos, recorder = Repos("lh-a-1"), MemoryRecorder()
    ended(recorder, "lh-a-1", 8 * DAY)

    result = await sweep(janitor(repos, recorder))

    assert repos.removed == ["lh-a-1"]
    assert result == acts.JanitorResult(removed=["lh-a-1"], kept=0, failed=0)


async def test_the_repo_of_a_task_that_ended_six_days_ago_stays() -> None:
    repos, recorder = Repos("lh-a-1"), MemoryRecorder()
    ended(recorder, "lh-a-1", 6 * DAY)

    result = await sweep(janitor(repos, recorder))

    assert repos.removed == []
    assert result == acts.JanitorResult(removed=[], kept=1, failed=0)


async def test_seven_days_to_the_second_is_not_more_than_seven_days() -> None:
    repos, recorder = Repos("lh-a-1"), MemoryRecorder()
    ended(recorder, "lh-a-1", acts.REPO_RETENTION)

    await sweep(janitor(repos, recorder))

    assert repos.removed == []


async def test_the_repo_of_a_task_that_is_still_running_is_never_removed() -> None:
    # It has no outcome yet. However old its repo is, the task may be waiting for a hint.
    repos, recorder = Repos("lh-running-1"), MemoryRecorder()

    result = await sweep(janitor(repos, recorder))

    assert repos.removed == []
    assert result.kept == 1


async def test_only_the_old_repos_go_and_the_others_stay() -> None:
    repos = Repos("lh-old-1", "lh-new-2", "lh-running-3", "lh-old-4")
    recorder = MemoryRecorder()
    ended(recorder, "lh-old-1", 30 * DAY)
    ended(recorder, "lh-new-2", 1 * DAY)
    ended(recorder, "lh-old-4", 7 * DAY + timedelta(seconds=1))

    result = await sweep(janitor(repos, recorder))

    assert sorted(repos.removed) == ["lh-old-1", "lh-old-4"]
    assert sorted(repos.ids) == ["lh-new-2", "lh-running-3"]
    assert (sorted(result.removed), result.kept) == (["lh-old-1", "lh-old-4"], 2)


async def test_a_second_sweep_has_nothing_to_do() -> None:
    repos, recorder = Repos("lh-a-1"), MemoryRecorder()
    ended(recorder, "lh-a-1", 9 * DAY)
    runner = janitor(repos, recorder)

    await sweep(runner)
    again = await sweep(runner)

    assert again == acts.JanitorResult(removed=[], kept=0, failed=0)


async def test_a_repo_that_cannot_be_removed_does_not_stop_the_sweep() -> None:
    repos = Repos("lh-a-1", "lh-b-2")
    repos.broken.add("lh-a-1")
    recorder = MemoryRecorder()
    ended(recorder, "lh-a-1", 9 * DAY)
    ended(recorder, "lh-b-2", 9 * DAY)

    result = await sweep(janitor(repos, recorder))

    assert repos.removed == ["lh-b-2"]
    assert result == acts.JanitorResult(removed=["lh-b-2"], kept=0, failed=1)


async def test_a_worker_with_no_repos_sweeps_nothing() -> None:
    assert await sweep(janitor(Repos(), MemoryRecorder())) == acts.JanitorResult(
        removed=[], kept=0, failed=0
    )


async def test_the_outcome_event_is_the_end_of_a_task() -> None:
    # The workflow notes the end with this event (LH-704). The recorder dates it.
    recorder = MemoryRecorder()
    await recorder.record_event("lh-a-1", OUTCOME_EVENT, {"kind": "stuck"})
    await recorder.record_event("lh-b-2", "stuck", {"reason": "no_progress"})

    assert await recorder.ended_before(["lh-a-1", "lh-b-2"], datetime.now(UTC) + DAY) == {"lh-a-1"}
    assert await recorder.ended_before(["lh-a-1"], datetime.now(UTC) - DAY) == set()


def test_the_outcome_event_name_is_the_one_the_workflow_writes() -> None:
    assert OUTCOME_EVENT == acts.OUTCOME_EVENT


# ---------- the folder of bare repos ----------
async def test_the_store_lists_the_task_ids_of_its_repos(tmp_path: Path) -> None:
    store = GitCheckpointStore(MemoryBlobStore(), root=tmp_path / "ckpt")
    assert await store.task_ids() == []  # the folder does not exist yet

    root = store.root
    root.mkdir(parents=True)
    (root / "lh-a-1.git").mkdir()
    (root / "lh-b-2.git").mkdir()
    (root / ".lh-c-3.ab12cd34.tmp").mkdir()  # a half-built clone
    (root / "notes.txt").write_text("not a repo")
    (root / "..odd.git").mkdir()  # not a valid task id
    (root / "plain-folder").mkdir()

    assert await store.task_ids() == ["lh-a-1", "lh-b-2"]
