"""A script that travels with a task: uploaded by `longhaul run`, played by any worker.

The blob store is the memory one, so these tests need no MinIO. tests/e2e/test_durable_run.py
sends a script through the real store and a worker in another process.
"""

from pathlib import Path

import pytest
from longhaul.durable.scripts import TaskProviders, script_key, upload_script

from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import Message, TextBlock
from longhaul.providers.scripted import ScriptedProvider

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

ROOT = Path(__file__).resolve().parents[2]
CALC_FIX = ROOT / "tests" / "fixtures" / "scripts" / "calc_fix.yaml"
TASK = "lh-local-calc-0a1b2c3d"
OTHER = "lh-local-calc-4e5f6a7b"
SYSTEM = "Role: coder\nFix the issue."
FIRST = [Message(role="user", content=[TextBlock(text="Start.")])]


def write_script(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_the_script_lives_under_the_folder_of_its_task() -> None:
    assert script_key(TASK) == f"tasks/{TASK}/script.yaml"


@pytest.mark.parametrize("task_id", ["", "../other", "a/b", ".hidden", "x" * 200])
def test_a_task_id_that_is_not_a_safe_name_has_no_script_key(task_id: str) -> None:
    with pytest.raises(ValueError, match="invalid task id"):
        script_key(task_id)


async def test_an_uploaded_script_is_stored_byte_for_byte() -> None:
    blobs = MemoryBlobStore()
    ref = await upload_script(blobs, TASK, CALC_FIX)

    assert ref.key == script_key(TASK)
    assert await blobs.get(ref) == CALC_FIX.read_bytes()


async def test_a_script_that_does_not_load_is_refused_before_anything_is_stored(
    tmp_path: Path,
) -> None:
    bad = write_script(tmp_path / "bad.yaml", "steps: [respond: {text: x}, respond: {text: y}\n")
    blobs = MemoryBlobStore()

    with pytest.raises(ValueError, match="bad.yaml"):
        await upload_script(blobs, TASK, bad)
    assert blobs.objects == {}


async def test_the_provider_built_from_a_ref_answers_like_one_built_from_the_file() -> None:
    blobs = MemoryBlobStore()
    ref = await upload_script(blobs, TASK, CALC_FIX)

    shipped = await TaskProviders(blobs).provider_for(TASK, ref)
    local = ScriptedProvider(CALC_FIX)

    args = {"model": "m", "system": SYSTEM, "messages": FIRST, "tools": [], "max_tokens": 100}
    assert (await shipped.complete(**args)).tool_calls == (await local.complete(**args)).tool_calls


async def test_a_task_gets_the_same_provider_on_every_call() -> None:
    blobs = MemoryBlobStore()
    ref = await upload_script(blobs, TASK, CALC_FIX)
    providers = TaskProviders(blobs)

    first = await providers.provider_for(TASK, ref)
    assert await providers.provider_for(TASK, ref) is first


async def test_each_task_gets_a_provider_of_its_own() -> None:
    blobs = MemoryBlobStore()
    providers = TaskProviders(blobs)
    mine = await providers.provider_for(TASK, await upload_script(blobs, TASK, CALC_FIX))
    theirs = await providers.provider_for(OTHER, await upload_script(blobs, OTHER, CALC_FIX))
    assert mine is not theirs


async def test_the_oldest_provider_is_dropped_when_the_cache_is_full() -> None:
    blobs = MemoryBlobStore()
    providers = TaskProviders(blobs, capacity=1)
    first_ref = await upload_script(blobs, TASK, CALC_FIX)
    first = await providers.provider_for(TASK, first_ref)
    await providers.provider_for(OTHER, await upload_script(blobs, OTHER, CALC_FIX))

    assert await providers.provider_for(TASK, first_ref) is not first


async def test_a_ref_to_another_tasks_script_is_refused() -> None:
    blobs = MemoryBlobStore()
    theirs = await upload_script(blobs, OTHER, CALC_FIX)

    with pytest.raises(ValueError, match="not a script of task"):
        await TaskProviders(blobs).provider_for(TASK, theirs)


async def test_a_script_that_was_damaged_in_the_store_fails_with_the_loader_message(
    tmp_path: Path,
) -> None:
    blobs = MemoryBlobStore()
    ref = await blobs.put(b"steps: 3\n", key=script_key(TASK))

    with pytest.raises(ValueError, match="steps"):
        await TaskProviders(blobs).provider_for(TASK, ref)
