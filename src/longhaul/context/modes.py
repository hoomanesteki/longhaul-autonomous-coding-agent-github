"""The context modes: which of the two context tiers a run uses (LH-406, ADR-0013).

The product runs both tiers. The evaluation needs to switch them off, one at a time, to measure
what each one buys (plan section 8, question 3), so the tiers are settings and not constants:

| mode            | tier 1: elide old tool outputs | tier 2: compact into notes |
|-----------------|--------------------------------|----------------------------|
| `full`          | on                             | on                         |
| `no_compaction` | on                             | off                        |
| `none`          | off                            | off                        |

What no mode switches off is the guard in front of the model call. A request projected over
`context.hard_limit` of the window is not sent, in every mode (ADR-0013). With tier 2 on, the
workflow answers that refusal with one forced compaction. With it off there is nothing to answer
with, so the run ends with the outcome `error` and the summary "context limit". That is how a task
fails in `none` and in `no_compaction`: cleanly, with a stored transcript and no crash.

Where a mode is read. `llm_turn` reads it for tier 1 and for its decision whether the next turn
compacts, and the `compact` activity reads it before it calls the compactor. The workflow reads it
only to skip a call it knows would do nothing. The config of the worker (`context.mode`) is the
default, and a run can name its own: it travels in the workflow memo
(acts.CONTEXT_MODE_MEMO_KEY) and from there in the input of each of those activities, which use
it in place of the config's.

This module is pure, so workflow code may import it (invariant I1).
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["ContextMode"]


class ContextMode(StrEnum):
    """The value is the name used in the config, on the command line and in the workflow memo."""

    FULL = "full"
    NO_COMPACTION = "no_compaction"
    NONE = "none"

    @property
    def elides(self) -> bool:
        """True when tool outputs older than the configured number of turns become stubs."""
        return self is not ContextMode.NONE

    @property
    def compacts(self) -> bool:
        """True when the older turns of a view may be folded into progress notes."""
        return self is ContextMode.FULL
