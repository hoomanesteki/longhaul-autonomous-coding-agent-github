"""The pure parts of how a task ends (LH-507).

TaskWorkflow winds a task up in a fixed order, whatever way it ended: the outcome is set, the
sandbox is destroyed, a draft pull request may open for partial work, the final comment is posted
and the final labels are set. The order and the calls are in the workflow. This module holds the
three things the workflow needs that are text:

* `error_trace` turns an exception into the trace that goes to the `events` table. The outcome
  summary says the type and nothing else, because the message of a provider error can echo the
  request. A person who wants the cause reads the event.
* `draft_summary` is the summary of the partial pull request of a stuck task.
* `cleanup_reason` is what the log of the cleanup activity says.

It has no I/O, reads no clock and draws no random number, so the workflow may import it
(invariant I1) and a replay gets the same text.
"""

from __future__ import annotations

from types import TracebackType

from longhaul.contracts import Outcome, OutcomeKind
from longhaul.durable import acts

__all__ = [
    "DRAFT_SUMMARY_CHARS",
    "MAX_MESSAGE_CHARS",
    "TRACE_CUT",
    "cleanup_reason",
    "draft_summary",
    "error_trace",
]

# What a trace that was cut starts with. The end of a trace is the part that names the error and
# the code that raised it, so the front is what goes.
TRACE_CUT = "... (earlier lines of the trace cut)\n"
# The longest message of one error in a trace. A provider error can echo a whole request, and the
# start of a message says what went wrong.
MESSAGE_CUT = "... (message cut)"
MAX_MESSAGE_CHARS = 600
# The longest summary of a partial pull request. It becomes the title of the branch and the
# summary section of the body, and the reason a task is stuck is a sentence.
DRAFT_SUMMARY_CHARS = 300


def _frames(tb: TracebackType | None) -> list[str]:
    """One line for each frame, outermost first: `folder/file.py:12 in function`.

    traceback.format_exception reads the source files to print the line of code, and a workflow
    reads no files. The file, the line and the function name are enough to find the line.
    """
    lines = []
    while tb is not None:
        code = tb.tb_frame.f_code
        where = "/".join(code.co_filename.replace("\\", "/").split("/")[-2:])
        lines.append(f"  {where}:{tb.tb_lineno} in {code.co_name}")
        tb = tb.tb_next
    return lines


def _chain(err: BaseException) -> list[BaseException]:
    """`err` and what caused it, the root cause first, as Python prints them."""
    found: list[BaseException] = []
    current: BaseException | None = err
    while current is not None and current not in found:
        found.append(current)
        cause = current.__cause__
        current = (
            cause
            if cause is not None
            else (None if current.__suppress_context__ else current.__context__)
        )
    return found[::-1]


def error_trace(err: BaseException) -> str:
    """The trace of `err` with the errors that caused it, cut to ERROR_TRACE_CHARS characters.

    The cut keeps the end. The chain is written with the root cause first and the error that was
    raised last, so the end holds the error the workflow saw. An application error shows its
    `type`, which is what the retry policies and the workflow match on.
    """
    parts = []
    for one in _chain(err):
        kind = getattr(one, "type", None)
        label = type(one).__name__ + (f" [{kind}]" if isinstance(kind, str) and kind else "")
        message = str(one)
        if len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + MESSAGE_CUT
        parts.append("\n".join([*_frames(one.__traceback__), f"{label}: {message}"]))
    trace = "\n\n".join(parts)
    if len(trace) <= acts.ERROR_TRACE_CHARS:
        return trace
    return TRACE_CUT + trace[len(trace) - (acts.ERROR_TRACE_CHARS - len(TRACE_CUT)) :]


def draft_summary(outcome: Outcome) -> str:
    """The summary of the draft pull request that holds the partial work of a task that gave up."""
    text = f"Partial work. The task is {outcome.kind.value}: {' '.join(outcome.summary.split())}."
    if len(text) <= DRAFT_SUMMARY_CHARS:
        return text
    return text[: DRAFT_SUMMARY_CHARS - 3].rstrip() + "..."


def cleanup_reason(kind: OutcomeKind) -> str:
    return f"the task ended: {kind.value}"
