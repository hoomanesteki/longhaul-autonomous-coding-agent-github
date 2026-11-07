"""The compact activity: the older turns of a view become progress notes (LH-404, tier 2).

The workflow calls it when llm_turn says the next request is getting too big ("compact"), is
already too big ("force"), or when the provider refused a prompt as too long (F6). The activity
loads the view, picks the cut with context.compact.find_cut, asks the compactor model for the
notes, replaces the turns before the cut with them (compact.apply) and stores the new view. It
returns the new ref, the notes to carry, and the tokens of the whole request before and after.

How much stays. A proactive compaction keeps `context.keep_recent_turns` turns. A forced one
keeps acts.FORCED_KEEP_RECENT_TURNS, which is 2, because the window has no room to wait. When the
view has no turn older than the ones to keep, there is nothing to fold: the activity answers
"unchanged" with the view it was given, and makes no model call. The workflow does not ask the
turn again in that case, since asking cannot help.

The modes (LH-406). Only `full` compacts (longhaul.context.modes). In `no_compaction` and `none`
the activity answers "unchanged" with the view it was given and makes no model call, whether the
config says so or the run asked for it in `CompactInput.context_mode`, which wins over the config.
The workflow skips the call when it knows the run's mode, and asks when it does not (the config is
the worker's), so a forced compaction after an overflow ends the run at once in those modes.

The compactor is context.compact.compact, with the model and effort of the compactor role. If it
gives no valid notes twice (F14), the previous notes stay and the cut is halved (halve_cut), so
fewer turns go without a summary. The previous notes are never dropped: compact() puts back any
`tried_and_failed` entry the model lost.

Hints. A hint shown to the model is a text block in a user message, and turns.show_hints adds
the hints of the task again when their text is missing from the transcript. A compaction that
folded a hint away would make every hint come back after it. So the hint texts of the folded
turns move into the first message, where they stay through later compactions (apply keeps the
task blocks of message 0). The compactor also reads them in the rendered turns, so they are in
the notes as well.

Invariant I6. Every call the compactor makes is on the cost ledger with a row of its own. compact()
reports each billed call to `on_billed` as soon as it is known, and the activity writes the row
there. A call that is billed and then fails, a refused force that was billed, and a compact()
that fails after its first call are all covered, and Temporal's retry of the activity adds rows
with the next attempt number. Rows are not summed: Haiku's price changes with the size of one
prompt, so a sum can be priced above the sum of its calls.

Blob keys. The new view is `turns/<turn>/transcript-compact-<attempt>.json`, or
`transcript-forced-<attempt>.json` for a forced one. A forced compaction before the model call of
a turn and a proactive one after it belong to the same turn, and neither may overwrite the
other.

Who answers. As in llm_turn, a task with a script is answered by it, so a scripted run gets a
scripted compactor (`match: {role: compactor}`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal
from typing import Any

from temporalio import activity

from longhaul.agent.loop import LoopConfig
from longhaul.agent.prompts import build_system_prompt, task_statement
from longhaul.agent.turns import HINT_PREFIX, TranscriptError
from longhaul.blobs.base import BlobStoreBase, blob_key
from longhaul.context.compact import (
    Compaction,
    apply,
    compact,
    find_cut,
    halve_cut,
    render_for_compaction,
)
from longhaul.context.modes import ContextMode
from longhaul.contracts import (
    LLMProvider,
    Message,
    ProgressNotes,
    ProviderError,
    Role,
    TextBlock,
    ToolSpec,
    Usage,
)
from longhaul.cost.ledger import Ledger, NoLedger
from longhaul.cost.pricing import PricingError
from longhaul.durable import acts
from longhaul.durable.activities.support import (
    Meters,
    heartbeating,
    not_retried,
    provider_failure,
    provider_for_task,
    small,
)
from longhaul.durable.scripts import TaskProviders
from longhaul.providers.base import turn_index
from longhaul.settings import Context


class ContextActivities:
    """The compact activity and what it needs.

    `provider`, `tools` and `config_for` are those of LlmActivities: the provider answers tasks
    without a script, the tools and the system prompt are what the token counts are made of, and
    `config_for(Role.COMPACTOR)` gives the compactor's model and effort. `context` is the policy
    of the context section, of which only `keep_recent_turns` is used here. `meters` is shared
    with the model turns of the worker. `ledger` is the cost ledger, as in LlmActivities.
    """

    def __init__(
        self,
        provider: LLMProvider | None,
        blobs: BlobStoreBase,
        tools: Sequence[ToolSpec],
        config_for: Callable[[Role], LoopConfig],
        context: Context,
        *,
        ledger: Ledger | None = None,
        meters: Meters | None = None,
    ) -> None:
        if context.keep_recent_turns < 1:
            raise ValueError(
                f"context.keep_recent_turns has to be at least 1, got {context.keep_recent_turns}"
            )
        self._provider = provider
        self._scripts = TaskProviders(blobs)
        self._blobs = blobs
        self._tools = list(tools)
        self._config_for = config_for
        self._context = context
        self._ledger: Ledger = NoLedger() if ledger is None else ledger
        self._meters = meters if meters is not None else Meters()

    @property
    def activities(self) -> list[Callable[..., Any]]:
        """What to register on a worker."""
        return [self.compact]

    @activity.defn(name=acts.COMPACT.name)
    async def compact(self, inp: acts.CompactInput) -> acts.CompactOutput:
        inp = small(inp)
        view = await self._blobs.get_json(inp.transcript_ref, list[Message])
        keep = acts.FORCED_KEEP_RECENT_TURNS if inp.forced else self._context.keep_recent_turns
        cut = find_cut(view, keep)
        system = build_system_prompt(inp.role, inp.spec)
        meter = self._meters.for_task(inp.task_id)
        before = meter.projected(system, view, self._tools)
        if cut == 0 or not self._mode(inp.context_mode).compacts:
            return small(
                acts.CompactOutput(
                    transcript_ref=inp.transcript_ref,
                    notes=inp.notes,
                    status="unchanged",
                    tokens_before=before,
                    tokens_after=before,
                    folded_turns=0,
                )
            )
        provider = await provider_for_task(
            self._scripts, self._provider, inp.task_id, inp.script_ref
        )
        cfg = self._config_for(Role.COMPACTOR)
        spent: list[Decimal] = []

        async def ledger_row(usage: Usage) -> None:
            spent.append(await self._charge(inp, cfg.model, usage))

        try:
            async with heartbeating():
                result = await compact(
                    provider,
                    cfg.model,
                    inp.notes,
                    render_for_compaction(view[:cut]),
                    task=task_statement(inp.spec) if inp.notes is None else None,
                    effort=cfg.effort,
                    covers_turns_up_to=turn_index(view[:cut]),
                    on_billed=ledger_row,
                )
        except ProviderError as exc:
            raise provider_failure(exc) from exc
        cut = self._cut_for(view, cut, result)
        if cut == 0:
            # A fallback halved the cut to nothing. The calls were made and are on the ledger,
            # and the view stays as it is.
            return small(
                acts.CompactOutput(
                    transcript_ref=inp.transcript_ref,
                    notes=result.notes,
                    status=result.status,
                    tokens_before=before,
                    tokens_after=before,
                    folded_turns=0,
                    cost_usd=float(sum(spent, Decimal(0))),
                )
            )
        compacted = self._apply(view, cut, result.notes)
        key = blob_key(
            inp.task_id,
            "turns",
            inp.turn_index,
            f"transcript-{'forced' if inp.forced else 'compact'}-{activity.info().attempt}.json",
        )
        ref = small(await self._blobs.put_json(compacted, key))
        return small(
            acts.CompactOutput(
                transcript_ref=ref,
                notes=result.notes,
                status=result.status,
                tokens_before=before,
                tokens_after=meter.projected(system, compacted, self._tools),
                folded_turns=sum(m.role == "assistant" for m in view[:cut]),
                cost_usd=float(sum(spent, Decimal(0))),
            )
        )

    def _mode(self, asked: ContextMode | None) -> ContextMode:
        """The mode of this call: the one the run asked for, else the one in the config."""
        return asked if asked is not None else self._context.mode

    @staticmethod
    def _cut_for(view: list[Message], cut: int, result: Compaction) -> int:
        """The cut to apply: the one asked for, or half of it after a fallback (F14).

        Without fresh notes every turn that goes is lost, so a fallback cuts less. Half of a cut
        can find no turn to fold, and the result is then 0: the view stays as it is, and the
        workflow is not failed for a compactor that did not work.
        """
        return halve_cut(view, cut) if result.status == "fallback" else cut

    @staticmethod
    def _apply(view: list[Message], cut: int, notes: ProgressNotes | None) -> list[Message]:
        try:
            return apply(_keep_hints(view, cut), cut, notes)
        except (ValueError, TranscriptError) as exc:
            # The cut came from find_cut, so this is a bug and asking again changes nothing.
            raise not_retried(exc) from exc

    async def _charge(self, inp: acts.CompactInput, model: str, usage: Usage) -> Decimal:
        """Put one compactor call on the ledger and return what it cost.

        A model without a price fails for good: a retry would call the compactor again, and none
        of its calls could be priced.
        """
        try:
            return await self._ledger.record(
                inp.task_id, inp.spec.repo, model, inp.turn_index, activity.info().attempt, usage
            )
        except PricingError as exc:
            raise not_retried(exc) from exc


def _keep_hints(view: list[Message], cut: int) -> list[Message]:
    """The view with the hint texts of the turns before `cut` added to its first message.

    The hint blocks are those of turns.show_hints. Each one is kept, also a text that appears
    twice, because show_hints counts a text as often as it was sent. Hints that are in the first
    message already came from an earlier compaction, and hints in the turns that stay are where
    they were.
    """
    hints = [
        block
        for message in view[1:cut]
        if message.role == "user"
        for block in message.content
        if isinstance(block, TextBlock) and block.text.startswith(HINT_PREFIX)
    ]
    if not hints:
        return view
    first = view[0]
    return [Message(role="user", content=[*first.content, *hints]), *view[1:]]
