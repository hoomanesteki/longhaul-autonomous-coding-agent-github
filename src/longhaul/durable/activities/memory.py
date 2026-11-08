"""The memory activities: the block a task starts with (LH-604), and the lessons it leaves (LH-605).

load_memory
-----------

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

extract_lessons
---------------
The workflow calls it once when a task has ended, on every ending path, after the final comment
and the labels, so no one waits for a model. The compactor model gets the plan, the last progress
notes, the stuck events and the issues of the verifier (longhaul.memory.lessons) and drafts up to
5 notes. They are stored as `candidate` items with `source = task` and the pull request number as
`source_ref`, or the task id when there is none. A person or a merged pull request approves them
later (ADR-0010, LH-606), and until then they stay out of every prompt.

The plan, the notes and the outcome come in with the call, since the workflow holds them. The
stuck events and the verdicts are read from the events table, because the workflow wrote them
there and keeps nothing else of them. A task with no plan, no notes and no such events has
nothing to learn from, and the activity makes no model call for it. A worker with no store
(`memory.enabled` off) makes none either, and answers "skipped".

Failures. A provider error is retried by kind, as in the other model calls. A store that cannot be
written raises, and Temporal runs the attempt again. The cleaned answer of the model is kept in a
blob (`lessons.json`, under the memory folder of the task) before the first write, so the next
attempt stores the same notes and asks no one: a model that is asked twice words its notes in two
ways, and the store would keep both sets. The call is billed once, with a ledger row (invariant
I6), and the kept answer carries its cost. The same note written twice is one row, since the
store keeps one row for one text. After the attempts are spent the workflow logs the failure and the
task keeps the outcome it had: lessons are a bonus and never change how a task ended.

As in every activity that calls a model, a task with a script is answered by it, so a scripted
run gets a scripted compactor (`match: {role: compactor}`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any, Protocol

from temporalio import activity

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.base import BlobIntegrityError, BlobNotFoundError, BlobStoreBase, blob_key
from longhaul.context.tokens import estimate_tokens
from longhaul.contracts import (
    BlobRef,
    BlobStore,
    LLMProvider,
    MemoryItem,
    ProviderError,
    Role,
    Usage,
)
from longhaul.cost.ledger import Ledger, NoLedger
from longhaul.cost.pricing import PricingError
from longhaul.db.records import EventRecord
from longhaul.durable import acts
from longhaul.durable.activities.support import (
    heartbeating,
    not_retried,
    provider_failure,
    provider_for_task,
    small,
)
from longhaul.durable.scripts import TaskProviders
from longhaul.memory import lessons
from longhaul.memory.retrieve import DEFAULT_BUDGET_TOKENS, MemoryReader, render_memory_block

log = logging.getLogger(__name__)

__all__ = [
    "BLOCK_NAME",
    "EventReader",
    "LessonActivities",
    "LessonSink",
    "MemoryActivities",
    "read_memory_block",
]

# The name of the blob, under the memory folder of the task. A retried attempt writes the same key.
BLOCK_NAME = "memory-block.txt"
# The answer of extract_lessons, kept so that a retried attempt stores what the first one got.
LESSONS_NAME = "lessons.json"


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


# ---------- the lessons of a finished task ----------
class LessonSink(Protocol):
    """Where the candidates go: PgMemoryStore.add_candidate, which keeps one row for one note."""

    async def add_candidate(self, item: MemoryItem, *, source_ref: str | None = None) -> str: ...


class EventReader(Protocol):
    """Where the activity reads the events of a task: the recorder (db.records)."""

    async def events_of(self, task_id: str, types: Sequence[str]) -> list[EventRecord]: ...


class LessonActivities:
    """extract_lessons, with the store it writes to and the compactor model it asks.

    `sink` is None on a worker that keeps no memory. `provider` answers tasks without a script,
    and `config_for(Role.COMPACTOR)` gives the model and effort of the compactor, as in
    ContextActivities. `ledger` is the cost ledger, shared with the other model calls.
    """

    def __init__(
        self,
        sink: LessonSink | None,
        events: EventReader,
        provider: LLMProvider | None,
        blobs: BlobStoreBase,
        config_for: Callable[[Role], LoopConfig],
        *,
        ledger: Ledger | None = None,
    ) -> None:
        self._sink = sink
        self._events = events
        self._provider = provider
        self._blobs = blobs
        self._scripts = TaskProviders(blobs)
        self._config_for = config_for
        self._ledger: Ledger = NoLedger() if ledger is None else ledger

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.extract_lessons]

    @activity.defn(name=acts.EXTRACT_LESSONS.name)
    async def extract_lessons(self, inp: acts.ExtractLessonsInput) -> acts.ExtractLessonsOutput:
        inp = small(inp)
        ref = lessons.source_ref(inp.outcome.pr_url, inp.task_id)
        skipped = acts.ExtractLessonsOutput(status="skipped", source_ref=ref)
        if self._sink is None:
            return skipped
        # A retry after a half-written store must write what the first attempt got, not what a
        # model says on a second ask. The answer is kept before any write for that reason.
        key = blob_key(inp.task_id, "memory", 0, LESSONS_NAME)
        got = await self._asked_before(key)
        if got is None:
            got = await self._ask(inp, ref, key)
        if got is None:
            return skipped
        for lesson in got.items:
            await self._sink.add_candidate(
                MemoryItem(
                    id="",  # the store makes the id
                    repo=inp.spec.repo,
                    kind=lesson.kind,
                    content=lesson.content,
                    status="candidate",
                    source="task",
                ),
                source_ref=ref,
            )
        log.info("task %s: %d lessons stored for %s", inp.task_id, len(got.items), ref)
        return small(got)

    async def _asked_before(self, key: str) -> acts.ExtractLessonsOutput | None:
        """The answer an earlier attempt kept, or None when no attempt got that far."""
        try:
            data = await self._blobs.get_by_key(key)
        except BlobNotFoundError:
            return None
        try:
            return acts.ExtractLessonsOutput.model_validate_json(data)
        except ValueError as exc:
            # Only this activity writes the key, so this is damage. Asking again is the cure.
            log.warning(
                "the kept lessons at %s cannot be read, so the model is asked: %s", key, exc
            )
            return None

    async def _ask(
        self, inp: acts.ExtractLessonsInput, ref: str, key: str
    ) -> acts.ExtractLessonsOutput | None:
        """Ask the compactor model and keep its cleaned answer, or None with nothing to ask."""
        stuck, issues = lessons.signals_from_events(
            await self._events.events_of(inp.task_id, lessons.EVENT_TYPES)
        )
        if inp.plan is None and inp.notes is None and not stuck and not issues:
            # Nothing happened that a note could be about, such as a task cancelled at once.
            return None
        provider = await provider_for_task(
            self._scripts, self._provider, inp.task_id, inp.script_ref
        )
        cfg = self._config_for(Role.COMPACTOR)
        spent: list[Decimal] = []

        async def ledger_row(usage: Usage) -> None:
            spent.append(await self._charge(inp, cfg.model, usage))

        request = lessons.build_request(
            title=inp.spec.title,
            outcome=inp.outcome,
            plan=inp.plan,
            notes=inp.notes,
            stuck=stuck,
            issues=issues,
        )
        try:
            async with heartbeating():
                found = await lessons.extract(
                    provider, cfg.model, request, effort=cfg.effort, on_billed=ledger_row
                )
        except ProviderError as exc:
            raise provider_failure(exc) from exc
        answer = small(
            acts.ExtractLessonsOutput(
                status=found.status,
                source_ref=ref,
                items=[acts.LessonNote(kind=n.kind, content=n.content) for n in found.lessons],
                cost_usd=float(sum(spent, Decimal(0))),
            )
        )
        await self._blobs.put(answer.model_dump_json().encode(), key=key)
        return answer

    async def _charge(self, inp: acts.ExtractLessonsInput, model: str, usage: Usage) -> Decimal:
        """Put one call on the ledger and return what it cost.

        A model without a price fails for good: a retry would call the model again, and none of
        its calls could be priced.
        """
        try:
            return await self._ledger.record(
                inp.task_id, inp.spec.repo, model, inp.turn_index, activity.info().attempt, usage
            )
        except PricingError as exc:
            raise not_retried(exc) from exc
