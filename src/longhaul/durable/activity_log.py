"""One log line when an activity starts, one when it ends or fails.

A worker that stops without warning leaves little behind. The line it logged last says what it
was doing: an activity that started and never ended was cut off, and an activity that ended just
before means the worker was between two. The chaos test reads exactly this to say where each of
its kills landed (tests/chaos), and an operator can read it to see what a worker did before it was
stopped.

    activity tool_exec started: task lh-local-inventory-1a2b3c4d, attempt 1, turn 8, run_command
    activity tool_exec ended: task lh-local-inventory-1a2b3c4d, attempt 1, turn 8, run_command
    activity llm_turn failed: task lh-local-inventory-1a2b3c4d, attempt 2, turn 3 (ProviderError)

A line names the activity, the task, the Temporal attempt and, for the two activities that have
them, the turn and the tool. It never holds arguments or output, which can hold code and secrets.

This is an interceptor and not a line in each activity, so that every activity is covered, a new
one included, and so that the format is written in one place. ACTIVITY_LINE is the pattern that
reads it back.
"""

import logging
import re
from collections.abc import Sequence
from typing import Any

from temporalio import activity
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput, Interceptor

from longhaul.durable import acts

log = logging.getLogger(__name__)

ACTIVITY_LINE = re.compile(
    r"activity (?P<name>\w+) (?P<event>started|ended|failed): "
    r"task (?P<task>\S+), attempt (?P<attempt>\d+)(?P<detail>[^\n]*)"
)


def describe(args: Sequence[Any]) -> str:
    """The turn, and the tool of a tool call, as the text that follows the attempt."""
    for arg in args:
        if isinstance(arg, acts.ToolExecInput):
            return f", turn {arg.turn_index}, {arg.call.name}"
        if isinstance(arg, acts.LlmTurnInput):
            return f", turn {arg.turn_index}"
    return ""


class _LoggingInbound(ActivityInboundInterceptor):
    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        info = activity.info()
        where = f"task {info.workflow_id}, attempt {info.attempt}{describe(input.args)}"
        log.info("activity %s started: %s", info.activity_type, where)
        try:
            result = await self.next.execute_activity(input)
        except BaseException as exc:  # a cancel is logged too, and goes on
            log.info("activity %s failed: %s (%s)", info.activity_type, where, type(exc).__name__)
            raise
        log.info("activity %s ended: %s", info.activity_type, where)
        return result


class ActivityLogInterceptor(Interceptor):
    """Worker interceptor that logs every activity it runs, local ones included."""

    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return _LoggingInbound(next)
