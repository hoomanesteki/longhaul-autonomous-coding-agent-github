"""The load_memory activity: the memory block of a task, written once for the whole task (LH-604).

The workflow calls it once, after prepare_workspace, and keeps the ref it returns. Every model
turn and every compaction reads that blob and puts the text at the end of the system prompt
(block 4 of agent/prompts.py), so the prompt is the same bytes in every request of the task and
the prompt cache keeps working (ADR-0013). That is why the block is written once and not asked
for on each turn: the store changes while a task runs, as a lesson is approved or a person
rejects a note, and a prompt that changed with it would drop the cache of the whole conversation.

The activity reads the repo map that prepare_workspace wrote for this issue (`repo_map_ref`),
asks the store for the repo's approved commands and for the lessons nearest to the issue, and
renders them under a budget (longhaul.memory.retrieve). An empty block is written as an empty
blob, so the workflow and the turns treat every task alike.

A store that cannot be reached raises, and Temporal tries again (3 attempts, section 3.7). When
those are spent, the workflow carries on without a block: the notes are hints, and a task that
runs without them is the task of before Phase 6. A repo map that is gone from the blob store is
left out and logged, since asking again would not bring it back. A worker with no store
(`memory.enabled` off) answers with an empty block, which the workflow does not ask for in the
first place (acts.PrepareWorkspaceOutput.memory_enabled).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from temporalio import activity

from longhaul.blobs.base import BlobIntegrityError, BlobNotFoundError, blob_key
from longhaul.context.tokens import estimate_tokens
from longhaul.contracts import BlobRef, BlobStore
from longhaul.durable import acts
from longhaul.durable.activities.support import small
from longhaul.memory.retrieve import DEFAULT_BUDGET_TOKENS, MemoryReader, render_memory_block

log = logging.getLogger(__name__)

__all__ = ["BLOCK_NAME", "MemoryActivities", "read_memory_block"]

# The name of the blob, under the memory folder of the task. A retried attempt writes the same key.
BLOCK_NAME = "memory-block.txt"


class MemoryActivities:
    """load_memory, with the store it reads and the blob store it writes to.

    `memory` is None on a worker that keeps no memory. `budget_tokens` caps the block.
    """

    def __init__(
        self,
        memory: MemoryReader | None,
        blobs: BlobStore,
        *,
        budget_tokens: int = DEFAULT_BUDGET_TOKENS,
    ) -> None:
        self._memory = memory
        self._blobs = blobs
        self._budget_tokens = budget_tokens

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.load_memory]

    @activity.defn(name=acts.LOAD_MEMORY.name)
    async def load_memory(self, inp: acts.LoadMemoryInput) -> BlobRef:
        inp = small(inp)
        spec = inp.spec
        block = ""
        if self._memory is not None:
            block = await render_memory_block(
                self._memory,
                spec.repo,
                f"{spec.title}\n\n{spec.body}",
                repo_map=await self._repo_map(inp.repo_map_ref),
                budget_tokens=self._budget_tokens,
            )
        key = blob_key(spec.task_id, "memory", 0, BLOCK_NAME)
        ref = await self._blobs.put(block.encode(), key=key)
        log.info(
            "memory block of task %s: about %d tokens for %s",
            spec.task_id,
            estimate_tokens(block),
            spec.repo,
        )
        return ref

    async def _repo_map(self, ref: BlobRef | None) -> str:
        if ref is None:
            return ""
        try:
            return (await self._blobs.get(ref)).decode("utf-8", errors="replace")
        except (BlobNotFoundError, BlobIntegrityError) as exc:
            # The map is a hint. The commands and the lessons are still worth sending.
            log.warning("could not read the repo map %s, so the block has none: %s", ref.key, exc)
            return ""


async def read_memory_block(blobs: BlobStore, ref: BlobRef | None) -> str:
    """The text of a task's memory block, or the empty string for a task that has none.

    The model turns and the compactions of a task call this with the ref the workflow carries,
    and all of them get the same text, since the blob is written once.
    """
    if ref is None:
        return ""
    return (await blobs.get(ref)).decode("utf-8")
