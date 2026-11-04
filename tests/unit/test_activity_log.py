"""The activity log lines that say where a worker was (LH-209).

The chaos test reads them to learn what a worker was doing when it was killed, so their shape is
part of the contract: `activity <name> <started|ended|failed>: task <id>, attempt <n>[, detail]`.
"""

import logging
from dataclasses import replace
from typing import Any

import pytest
from longhaul.durable.activity_log import ACTIVITY_LINE, ActivityLogInterceptor, describe
from temporalio.testing import ActivityEnvironment
from temporalio.worker import ActivityInboundInterceptor, ExecuteActivityInput

from longhaul.contracts import BlobRef, Role, TaskSpec
from longhaul.durable import acts

pytestmark = [pytest.mark.unit, pytest.mark.phase2]

LOGGER = "longhaul.durable.activity_log"
REF = BlobRef(key="tasks/lh-t-1/x.json", sha256="a" * 64, size=1)


class Next(ActivityInboundInterceptor):
    """The end of the chain: returns `result`, or raises `error`."""

    def __init__(self, result: Any = "done", error: BaseException | None = None) -> None:
        super().__init__(None)  # type: ignore[arg-type]
        self.result, self.error = result, error

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        if self.error is not None:
            raise self.error
        return self.result


def execute(args: list[Any], *, name: str, attempt: int = 1, tail: Next | None = None) -> Any:
    inbound = ActivityLogInterceptor().intercept_activity(tail or Next())
    env = ActivityEnvironment()
    env.info = replace(
        env.info, activity_type=name, workflow_id="lh-t-1", attempt=attempt, is_local=False
    )
    given = ExecuteActivityInput(fn=lambda: None, args=args, executor=None, headers={})
    return env.run(inbound.execute_activity, given)


def tool_input(tool: str = "run_command", turn: int = 7) -> acts.ToolExecInput:
    return acts.ToolExecInput(
        task_id="lh-t-1",
        turn_index=turn,
        call=acts.ToolCallSummary(id="toolu_1", name=tool, input={"command": "ls"}),
        pre_sha="b" * 40,
        sandbox_id="sbx",
    )


def llm_input(turn: int = 3) -> acts.LlmTurnInput:
    spec = TaskSpec(task_id="lh-t-1", repo="local:x", title="t", body="b")
    return acts.LlmTurnInput(
        task_id="lh-t-1", turn_index=turn, role=Role.CODER, spec=spec, transcript_ref=REF
    )


def messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


async def test_an_activity_logs_when_it_starts_and_when_it_ends(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        result = await execute([tool_input()], name="tool_exec", attempt=2)

    assert result == "done"
    assert messages(caplog) == [
        "activity tool_exec started: task lh-t-1, attempt 2, turn 7, run_command",
        "activity tool_exec ended: task lh-t-1, attempt 2, turn 7, run_command",
    ]


async def test_a_model_turn_names_its_turn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await execute([llm_input(3)], name="llm_turn")

    assert messages(caplog)[0] == "activity llm_turn started: task lh-t-1, attempt 1, turn 3"


async def test_an_activity_with_another_input_logs_no_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await execute([REF], name="append_results")

    assert messages(caplog)[0] == "activity append_results started: task lh-t-1, attempt 1"


async def test_a_failed_activity_logs_the_error_type_and_raises_it_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(KeyError):
        await execute(
            [tool_input("edit_file", 4)], name="tool_exec", tail=Next(error=KeyError("x"))
        )

    assert messages(caplog) == [
        "activity tool_exec started: task lh-t-1, attempt 1, turn 4, edit_file",
        "activity tool_exec failed: task lh-t-1, attempt 1, turn 4, edit_file (KeyError)",
    ]


def test_describe_leaves_out_what_a_log_must_not_hold() -> None:
    # The arguments of a call can hold code, and its output can hold secrets.
    detail = describe([tool_input()])
    assert detail == ", turn 7, run_command"
    assert "ls" not in detail


def test_the_lines_match_the_pattern_that_readers_use() -> None:
    line = "activity tool_exec started: task lh-t-1, attempt 2, turn 7, run_command"
    found = ACTIVITY_LINE.search(f"2026-11-04 08:00:00,000 INFO {LOGGER}: {line}")
    assert found is not None
    assert found.group("name", "event", "task", "attempt", "detail") == (
        "tool_exec",
        "started",
        "lh-t-1",
        "2",
        ", turn 7, run_command",
    )
    failed = ACTIVITY_LINE.search("activity cleanup failed: task t, attempt 1 (ValueError)")
    assert failed is not None and failed.group("event") == "failed"
