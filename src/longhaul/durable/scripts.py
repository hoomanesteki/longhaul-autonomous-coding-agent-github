"""A script that travels with a task, so one worker can play a different script for each task.

The scripted provider answers from a YAML file. In `longhaul run-local` that file is next to the
process. With the durable stack the model call happens in a worker, which is started once, in
another process and maybe on another machine, and then plays many tasks. The file therefore goes
with the task:

1. `longhaul run` calls `upload_script`, which checks that the script loads and stores it in the
   blob store under `tasks/<task_id>/script.yaml`.
2. It starts the workflow with the BlobRef in the memo (acts.SCRIPT_MEMO_KEY). The workflow hands
   the ref to every `llm_turn` (LlmTurnInput.script_ref).
3. The activity asks `TaskProviders` for the provider of the task. The first call downloads the
   script and builds a ScriptedProvider. The later calls reuse it.

Why a cache and not a new provider for each turn. A scripted provider keeps small counters, such
as how often a step that raises `times: N` has already raised, and a new provider on every turn
would start them again. A restarted worker starts them again too, which is fine: the counters
only drive tests of the retry policy.

The cache holds the last few tasks, so a worker that runs for months does not grow with the
number of tasks it has seen. A task whose provider was dropped downloads its script again.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections import OrderedDict
from pathlib import Path

from longhaul.blobs.base import BlobStoreBase, check_key
from longhaul.checkpoints.git_store import check_task_id
from longhaul.contracts import BlobRef, LLMProvider
from longhaul.providers.scripted import ScriptedProvider

SCRIPT_FILE = "script.yaml"
# How many tasks a worker keeps a provider for. A worker plays a handful of tasks at once.
DEFAULT_CAPACITY = 32


def script_key(task_id: str) -> str:
    """tasks/<task_id>/script.yaml. The id is checked, because it becomes part of a blob key."""
    return check_key(f"tasks/{check_task_id(task_id)}/{SCRIPT_FILE}")


async def upload_script(blobs: BlobStoreBase, task_id: str, path: Path) -> BlobRef:
    """Store the script of a task and return its ref.

    A script that does not load is refused here, before the task is started, with the message
    of the loader (it names the file). Otherwise the mistake would show up in a worker, one
    failed model turn later, with the task already running.
    """
    await asyncio.to_thread(ScriptedProvider, path)
    data = await asyncio.to_thread(path.read_bytes)
    return await blobs.put(data, key=script_key(task_id))


class TaskProviders:
    """The scripted provider of each task, built from the script in the blob store."""

    def __init__(self, blobs: BlobStoreBase, *, capacity: int = DEFAULT_CAPACITY) -> None:
        self._blobs = blobs
        self._capacity = capacity
        # Oldest first. The key holds the hash too, so a script that was replaced under the same
        # task id is not served from the cache.
        self._providers: OrderedDict[tuple[str, str], LLMProvider] = OrderedDict()

    async def provider_for(self, task_id: str, ref: BlobRef) -> LLMProvider:
        """The provider that plays the script `ref` names for the task.

        Raises ValueError when the ref is not the script of this task or the script does not
        load, and a BlobError when the store has no such blob or the bytes are damaged.
        """
        if ref.key != script_key(task_id):
            raise ValueError(f"{ref.key!r} is not a script of task {task_id}")
        cache_key = (task_id, ref.sha256)
        cached = self._providers.get(cache_key)
        if cached is not None:
            return cached
        provider = await asyncio.to_thread(_build, await self._blobs.get(ref))
        self._providers[cache_key] = provider
        while len(self._providers) > self._capacity:
            self._providers.popitem(last=False)
        return provider


def _build(data: bytes) -> LLMProvider:
    """A ScriptedProvider for script text. The loader reads a file, so it gets a temporary one."""
    with tempfile.TemporaryDirectory(prefix="lh-script-") as scratch:
        path = Path(scratch) / SCRIPT_FILE
        path.write_bytes(data)
        return ScriptedProvider(path)
