"""The extract_lessons activity (LH-605): lessons from a finished task, stored as candidates.

It runs inside temporalio's ActivityEnvironment with a fake provider, the in-memory recorder
that holds the task's events, a list that stands in for the memory store, and the memory ledger
on the checked-in prices. What matters: only candidates are written (invariant I7), each is
linked to the pull request or the task, every billed call has a ledger row (I6), and a task that
has nothing to learn from makes no model call.
"""

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    LLMResponse,
    MemoryItem,
    Outcome,
    OutcomeKind,
    Plan,
    PlanStep,
    ProviderError,
    ProviderErrorKind,
    RepoSettings,
    Role,
    TaskSpec,
    ToolCall,
    ToolUseBlock,
    Usage,
)
from longhaul.cost.ledger import MemoryLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.memory import LessonActivities
from longhaul.memory.lessons import TOOL_NAME

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

ROOT = Path(__file__).resolve().parents[2]
HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
PRICES = Pricing.load(ROOT / "config" / "pricing.yaml")
SPEC = TaskSpec(
    task_id="lh-acme-inventory-7",
    repo="acme/inventory",
    issue_number=7,
    title="remove() hides a KeyError",
    body="remove('x') raises KeyError",
    settings=RepoSettings(repo="acme/inventory"),
)
PLAN = Plan(
    summary="Make remove raise InventoryError.",
    steps=[
        PlanStep(
            id="s1",
            description="Raise InventoryError in remove",
            files=["src/inventory/core.py"],
            done_when="test_remove_missing passes",
            status="done",
        )
    ],
    acceptance_checks=["pytest -q"],
)
PR = "https://github.com/acme/inventory/pull/17"
LESSON = "Inventory.remove raises InventoryError, not KeyError, and test_inventory.py checks it."
PITFALL = "conftest.py builds the Inventory once per session, so a test that adds items leaks."
USAGE = Usage(input_tokens=900, output_tokens=120)


def config_for(role: Role) -> LoopConfig:
    return LoopConfig(model=HAIKU if role is Role.COMPACTOR else SONNET)


def reply(items: list[dict[str, str]], usage: Usage = USAGE) -> LLMResponse:
    block = ToolUseBlock(id="c1", name=TOOL_NAME, input={"items": items})
    return LLMResponse(
        content=[block],
        tool_calls=[ToolCall(id=block.id, name=block.name, input=block.input)],
        stop_reason="tool_use",
        usage=usage,
        model=HAIKU,
        latency_ms=1,
    )


class FakeProvider:
    name = "fake"

    def __init__(self, *steps: LLMResponse | ProviderError) -> None:
        self.steps = list(steps)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, ProviderError):
            raise step
        return step

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


class Sink:
    """The write side of the memory store: it keeps what was added and the source it names."""

    def __init__(self, *, fail: Exception | None = None, fail_after: int | None = None) -> None:
        self.added: list[tuple[MemoryItem, str | None]] = []
        self.fail = fail
        # A store that goes down after `fail_after` writes, once: the next attempt finds it up.
        self.fail_after = fail_after

    async def add_candidate(self, item: MemoryItem, *, source_ref: str | None = None) -> str:
        if self.fail is not None:
            raise self.fail
        if self.fail_after is not None and len(self.added) == self.fail_after:
            self.fail_after = None
            raise ConnectionError("the connection dropped")
        # The real store keeps one row for one text and one source.
        for number, (known, ref) in enumerate(self.added, start=1):
            if (known.content, ref) == (item.content, source_ref):
                return f"item-{number}"
        self.added.append((item, source_ref))
        return f"item-{len(self.added)}"


class Rig:
    def __init__(
        self, *steps: LLMResponse | ProviderError, sink: Sink | None | bool = True
    ) -> None:
        self.provider = FakeProvider(*steps)
        self.recorder = MemoryRecorder()
        self.ledger = MemoryLedger(PRICES)
        self.sink = Sink() if sink is True else sink
        assert not isinstance(self.sink, bool)
        self.activities = LessonActivities(
            self.sink,
            self.recorder,
            self.provider,
            MemoryBlobStore(),
            config_for,
            ledger=self.ledger,
        )

    async def run(self, inp: acts.ExtractLessonsInput, *, attempt: int = 1) -> Any:
        env = ActivityEnvironment()
        env.info = dataclasses.replace(env.info, attempt=attempt)
        return await env.run(self.activities.extract_lessons, inp)


def lesson_input(**fields: Any) -> acts.ExtractLessonsInput:
    values: dict[str, Any] = {
        "task_id": SPEC.task_id,
        "turn_index": 11,
        "spec": SPEC,
        "outcome": Outcome(kind=OutcomeKind.PR_OPENED, pr_url=PR, summary="Done."),
        "plan": PLAN,
    }
    return acts.ExtractLessonsInput(**{**values, **fields})


async def test_the_items_are_stored_as_candidates_linked_to_the_pull_request() -> None:
    rig = Rig(
        reply([{"kind": "lesson", "content": LESSON}, {"kind": "pitfall", "content": PITFALL}])
    )

    out = await rig.run(lesson_input())

    assert [(item.kind, item.status, item.source, item.repo) for item, _ in rig.sink.added] == [
        ("lesson", "candidate", "task", "acme/inventory"),
        ("pitfall", "candidate", "task", "acme/inventory"),
    ]
    assert [ref for _, ref in rig.sink.added] == ["17", "17"]
    assert [item.content for item, _ in rig.sink.added] == [LESSON, PITFALL]
    assert (out.status, out.source_ref, out.stored) == ("ok", "17", 2)
    assert [(n.kind, n.content) for n in out.items] == [("lesson", LESSON), ("pitfall", PITFALL)]


async def test_a_task_without_a_pull_request_is_linked_by_its_id() -> None:
    rig = Rig(reply([{"kind": "convention", "content": LESSON}]))

    out = await rig.run(
        lesson_input(outcome=Outcome(kind=OutcomeKind.STUCK, summary="no hint in 24 hours"))
    )

    assert [ref for _, ref in rig.sink.added] == [SPEC.task_id]
    assert out.source_ref == SPEC.task_id


async def test_generic_items_are_not_stored() -> None:
    rig = Rig(
        reply(
            [{"kind": "lesson", "content": "Write tests."}, {"kind": "lesson", "content": LESSON}]
        )
    )

    out = await rig.run(lesson_input())

    assert [item.content for item, _ in rig.sink.added] == [LESSON]
    assert out.stored == 1


async def test_the_model_sees_the_stuck_events_and_the_verifier_issues_of_this_task_only() -> None:
    rig = Rig(reply([]))
    await rig.recorder.record_event(
        SPEC.task_id,
        "stuck",
        {"reason": "repeated_failure", "evidence": ["run_tests failed 3 times"], "turn_index": 4},
    )
    await rig.recorder.record_event(
        SPEC.task_id, "paused", {"reason": "agent_asked", "question": "Which Inventory class?"}
    )
    await rig.recorder.record_event(
        SPEC.task_id, "verdict", {"round": 1, "passed": False, "issues": ["remove keeps the sku"]}
    )
    await rig.recorder.record_event(
        "lh-other-1", "stuck", {"reason": "edit_churn", "evidence": ["other"]}
    )

    await rig.run(lesson_input())

    (sent,) = rig.provider.calls
    request = sent["messages"][0].content[0].text
    assert "repeated_failure" in request
    assert "run_tests failed 3 times" in request
    assert "Which Inventory class?" in request
    assert "remove keeps the sku" in request
    assert "edit_churn" not in request
    assert "Raise InventoryError in remove" in request
    assert sent["model"] == HAIKU


async def test_every_billed_call_has_a_ledger_row_and_the_cost_is_reported() -> None:
    rig = Rig(reply([{"kind": "lesson", "content": LESSON}]))

    out = await rig.run(lesson_input(), attempt=2)

    assert [(r.task_id, r.model, r.turn_index, r.attempt) for r in rig.ledger.entries] == [
        (SPEC.task_id, HAIKU, 11, 2)
    ]
    assert out.cost_usd == pytest.approx(float(sum(r.usd for r in rig.ledger.entries)))
    assert out.cost_usd > 0


async def test_a_task_with_nothing_to_go_on_makes_no_model_call() -> None:
    rig = Rig()

    out = await rig.run(lesson_input(plan=None, notes=None))

    assert rig.provider.calls == []
    assert rig.sink.added == []
    assert (out.status, out.stored, out.cost_usd) == ("skipped", 0, 0.0)


async def test_a_worker_without_a_store_makes_no_model_call() -> None:
    rig = Rig(sink=None)

    out = await rig.run(lesson_input())

    assert rig.provider.calls == []
    assert out.status == "skipped"


async def test_a_reply_nobody_can_use_stores_nothing_and_does_not_fail() -> None:
    rig = Rig(reply([{"kind": "joke", "content": LESSON}]))

    out = await rig.run(lesson_input())

    assert rig.sink.added == []
    assert (out.status, out.stored) == ("ok", 0)


async def test_a_provider_error_fails_the_attempt_with_its_kind() -> None:
    rig = Rig(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))

    with pytest.raises(ApplicationError) as caught:
        await rig.run(lesson_input())

    assert caught.value.type == ProviderErrorKind.OVERLOADED.value
    assert not caught.value.non_retryable


async def test_a_store_that_cannot_be_written_fails_the_attempt_so_it_is_retried() -> None:
    rig = Rig(
        reply([{"kind": "lesson", "content": LESSON}]), sink=Sink(fail=ConnectionError("down"))
    )

    with pytest.raises(ConnectionError):
        await rig.run(lesson_input())


async def test_a_retry_after_a_partial_write_stores_the_first_answer_and_asks_no_more() -> None:
    first = [
        {"kind": "lesson", "content": LESSON},
        {"kind": "pitfall", "content": PITFALL},
        {
            "kind": "convention",
            "content": "Tests for Inventory live in tests/unit/test_inventory.py.",
        },
    ]
    # A model that is asked again words its notes in another way.
    second = [
        {
            "kind": "lesson",
            "content": "Inventory.remove now raises InventoryError for a missing sku.",
        },
        {
            "kind": "pitfall",
            "content": "The Inventory fixture in conftest.py is shared by the session.",
        },
        {"kind": "convention", "content": "Put Inventory tests in tests/unit/test_inventory.py."},
    ]
    rig = Rig(reply(first), reply(second), sink=Sink(fail_after=2))

    with pytest.raises(ConnectionError):
        await rig.run(lesson_input(), attempt=1)
    out = await rig.run(lesson_input(), attempt=2)

    assert [item.content for item, _ in rig.sink.added] == [n["content"] for n in first]
    assert len(rig.provider.calls) == 1
    assert [n.content for n in out.items] == [n["content"] for n in first]
    assert (out.status, out.source_ref) == ("ok", "17")
    # The first attempt paid for the call and has the only ledger row. The result still carries
    # its cost, since the workflow adds up what the activity reports.
    assert len(rig.ledger.entries) == 1
    assert out.cost_usd == pytest.approx(float(rig.ledger.entries[0].usd))


def test_the_activity_is_registered_under_the_name_the_workflow_calls() -> None:
    rig = Rig()

    assert rig.activities.extract_lessons in rig.activities.activities
    assert acts.EXTRACT_LESSONS in acts.ALL
