"""The cost ledger inside llm_turn (LH-701): a row for every call that was billed.

The setup is the one of test_activities_llm.py: the activity runs inside temporalio's
ActivityEnvironment with a fake provider, the memory blob store and the memory recorder, and
here also with the memory ledger. tests/integration/test_cost_ledger.py runs it with Postgres and
with retries by a real Temporal server.
"""

import dataclasses
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from longhaul.telemetry.metrics import Metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.agent.loop import LoopConfig
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.contracts import (
    BlobRef,
    LLMResponse,
    ProviderError,
    ProviderErrorKind,
    RepoSettings,
    Role,
    StopReason,
    TaskSpec,
    TextBlock,
    ToolCall,
    ToolUseBlock,
    Usage,
)
from longhaul.cost.ledger import Ledger, MemoryLedger
from longhaul.cost.pricing import Pricing
from longhaul.db.records import MemoryRecorder
from longhaul.durable import acts
from longhaul.durable.activities.llm import LlmActivities
from longhaul.telemetry import otel
from longhaul.tools.registry import default_registry

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

PRICING = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"
SONNET = "claude-sonnet-5-5"
CFG = LoopConfig(
    model=SONNET, max_turns=10, max_tokens_per_turn=1000, max_tokens_retry=5000, effort="medium"
)
SPEC = TaskSpec(
    task_id="lh-acme-calc-42",
    repo="acme/calc",
    issue_number=42,
    title="add() subtracts",
    body="add(2, 3) returns -1",
    settings=RepoSettings(repo="acme/calc"),
)
TOOLS = default_registry().specs()
# 1000 * 2.00 + 200 * 10.00 + 100 * 2.50 + 500 * 0.20 = 2000 + 2000 + 250 + 100 = 4350 dollars
# per million tokens, so 0.00435 for one call.
USAGE = Usage(input_tokens=1_000, output_tokens=200, cache_read_tokens=500, cache_write_tokens=100)
COST = Decimal("0.00435")


class FakeProvider:
    """Answers with the steps it was given, in order."""

    name = "fake"

    def __init__(self, *steps: LLMResponse | ProviderError) -> None:
        self.steps = list(steps)

    async def complete(self, **kwargs: Any) -> LLMResponse:
        assert self.steps, "the activity asked for more replies than the test gave"
        step = self.steps.pop(0)
        if isinstance(step, ProviderError):
            raise step
        return step

    async def count_tokens(self, **kwargs: Any) -> int:
        return 0


def reply(stop: StopReason, *blocks: Any, model: str = SONNET) -> LLMResponse:
    calls = [
        ToolCall(id=b.id, name=b.name, input=b.input) for b in blocks if isinstance(b, ToolUseBlock)
    ]
    return LLMResponse(
        content=list(blocks),
        tool_calls=calls,
        stop_reason=stop,
        usage=USAGE,
        model=model,
        latency_ms=42,
    )


def use(call_id: str, name: str = "read_file", **args: Any) -> ToolUseBlock:
    return ToolUseBlock(id=call_id, name=name, input=args)


class FlakyBlobs(MemoryBlobStore):
    """A blob store whose first write fails, as a lost connection would."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def put(self, data: bytes, *, key: str) -> BlobRef:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("the blob store is unreachable")
        return await super().put(data, key=key)


class BrokenLedger:
    """A ledger whose database is down."""

    async def record(
        self, task_id: str, repo: str, model: str, turn_index: int, attempt: int, usage: Usage
    ) -> Decimal:
        raise ConnectionError("the database is unreachable")


class Rig:
    """The activity with its fakes.

    It writes to a memory ledger on the real prices, unless a test gives it another ledger.
    """

    def __init__(
        self,
        *steps: LLMResponse | ProviderError,
        blobs: MemoryBlobStore | None = None,
        config: Callable[[Role], LoopConfig] | None = None,
        ledger: Ledger | None = None,
    ) -> None:
        self.provider = FakeProvider(*steps)
        self.recorder = MemoryRecorder()
        self.ledger = MemoryLedger(Pricing.load(PRICING))
        self.activities = LlmActivities(
            self.provider,
            blobs or MemoryBlobStore(),
            self.recorder,
            TOOLS,
            config or (lambda role: CFG),
            ledger=ledger or self.ledger,
        )

    async def run(self, *, turn_index: int = 0, attempt: int = 1) -> acts.LlmTurnOutput:
        env = ActivityEnvironment()
        env.info = dataclasses.replace(env.info, attempt=attempt)
        inp = acts.LlmTurnInput(
            task_id=SPEC.task_id,
            turn_index=turn_index,
            role=Role.CODER,
            spec=SPEC,
            transcript_ref=None,
        )
        return await env.run(self.activities.llm_turn, inp)


# ---------- what the row says ----------
async def test_a_call_is_ledgered_with_its_task_repo_model_turn_and_price() -> None:
    rig = Rig(reply("tool_use", use("toolu_1", path="a.py")))
    await rig.run(turn_index=4)

    (entry,) = rig.ledger.entries
    assert (entry.task_id, entry.repo, entry.model) == (SPEC.task_id, "acme/calc", SONNET)
    assert (entry.turn_index, entry.attempt) == (4, 1)
    assert entry.usage == USAGE
    assert entry.usd == COST


async def test_the_output_and_the_turn_row_carry_the_cost() -> None:
    rig = Rig(reply("tool_use", use("toolu_1", path="a.py")))
    out = await rig.run()

    assert out.cost_usd == pytest.approx(0.00435)
    (row,) = rig.recorder.turns
    assert row.cost_usd == pytest.approx(0.00435)


async def test_the_chat_span_carries_the_ledger_cost() -> None:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    rig = Rig(reply("tool_use", use("toolu_1", path="a.py")))
    try:
        with otel.configured(provider):
            await rig.run()
    finally:
        provider.shutdown()

    (span,) = [s for s in memory.get_finished_spans() if s.name.startswith("chat ")]
    assert span.attributes is not None
    assert span.attributes[otel.COST_USD] == pytest.approx(float(COST))


async def test_the_ledger_attempt_is_the_temporal_attempt() -> None:
    rig = Rig(reply("end_turn", TextBlock(text="Done.")))
    await rig.run(attempt=3)

    assert [e.attempt for e in rig.ledger.entries] == [3]
    # The turns table counts the calls of a turn instead, and this was the first.
    assert [t.attempt for t in rig.recorder.turns] == [1]


async def test_the_price_is_the_one_of_the_model_that_was_asked_for() -> None:
    # A reply may name the model by a dated id, which has no price. The request named the
    # configured one.
    rig = Rig(reply("end_turn", TextBlock(text="Done."), model=f"{SONNET}-20261001"))
    await rig.run()
    assert [(e.model, e.usd) for e in rig.ledger.entries] == [(SONNET, COST)]


# ---------- every call that was billed ----------
@pytest.mark.parametrize("stop", ["max_tokens", "pause_turn", "refusal"])
async def test_a_reply_that_is_not_stored_is_ledgered_all_the_same(stop: StopReason) -> None:
    rig = Rig(reply(stop, TextBlock(text="partial")))
    out = await rig.run()

    assert out.stop_reason == stop
    assert [e.usd for e in rig.ledger.entries] == [COST]


async def test_two_calls_for_one_turn_are_two_rows() -> None:
    rig = Rig(reply("max_tokens", TextBlock(text="cut")), reply("end_turn", TextBlock(text="ok")))
    await rig.run()
    await rig.run()
    assert [(e.turn_index, e.attempt) for e in rig.ledger.entries] == [(0, 1), (0, 1)]


async def test_a_billed_call_that_failed_is_ledgered_before_the_error_goes_up() -> None:
    billed = Usage(input_tokens=700, output_tokens=30)
    rig = Rig(ProviderError(ProviderErrorKind.SERVER, "body did not parse", usage=billed))
    with pytest.raises(ApplicationError):
        await rig.run(attempt=2)

    # 700 * 2.00 + 30 * 10.00 = 1700 per million tokens, so 0.0017.
    (entry,) = rig.ledger.entries
    assert (entry.usage, entry.usd, entry.attempt) == (billed, Decimal("0.0017"), 2)
    (row,) = rig.recorder.turns
    assert row.cost_usd == pytest.approx(0.0017)


async def test_a_call_that_failed_before_it_was_billed_has_no_row() -> None:
    rig = Rig(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))
    with pytest.raises(ApplicationError):
        await rig.run()
    assert rig.ledger.entries == []


async def test_a_call_is_ledgered_when_the_activity_fails_after_it_returned() -> None:
    rig = Rig(
        reply("end_turn", TextBlock(text="first")),
        reply("end_turn", TextBlock(text="second")),
        blobs=FlakyBlobs(),
    )
    with pytest.raises(ConnectionError):
        await rig.run(attempt=1)  # the reply came back, then the blob store failed
    assert [e.attempt for e in rig.ledger.entries] == [1]

    await rig.run(attempt=2)  # Temporal runs the activity again, and the model is called again
    assert [(e.attempt, e.usd) for e in rig.ledger.entries] == [(1, COST), (2, COST)]


# ---------- failures of the ledger itself ----------
async def test_a_ledger_that_cannot_write_fails_the_activity_so_that_it_is_retried() -> None:
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), ledger=BrokenLedger())
    with pytest.raises(ConnectionError):
        await rig.run()
    assert rig.recorder.turns == []  # nothing else was written for a call that is not on the ledger


async def test_a_model_without_a_price_fails_for_good_instead_of_being_retried() -> None:
    # Each retry would call the model again, and none of those calls could be priced.
    unpriced = LoopConfig(model="claude-opus-9", max_turns=10)
    rig = Rig(reply("end_turn", TextBlock(text="Done.")), config=lambda role: unpriced)
    with pytest.raises(ApplicationError) as caught:
        await rig.run()
    assert caught.value.non_retryable
    assert caught.value.type == "PricingError"
    assert "claude-opus-9" in str(caught.value)
    assert rig.ledger.entries == []


# ---------- a worker with no ledger ----------
async def test_without_a_ledger_nothing_is_recorded_and_the_cost_is_zero() -> None:
    rig = Rig(reply("end_turn", TextBlock(text="Done.")))
    bare = LlmActivities(rig.provider, MemoryBlobStore(), rig.recorder, TOOLS, lambda role: CFG)
    env = ActivityEnvironment()
    inp = acts.LlmTurnInput(
        task_id=SPEC.task_id, turn_index=0, role=Role.CODER, spec=SPEC, transcript_ref=None
    )

    out = await env.run(bare.llm_turn, inp)

    assert out.cost_usd == 0.0
    assert rig.ledger.entries == []


# ---------- the metrics of a call (LH-704) ----------
async def test_a_reply_is_counted_as_a_turn_with_its_cost_and_cache_ratio(
    counters: Metrics,
) -> None:
    rig = Rig(reply("tool_use", use("toolu_1", path="a.py")))
    await rig.run()

    reading = counters.registry.get_sample_value
    assert reading("longhaul_turns_total", {"role": "coder"}) == 1
    cost = reading("longhaul_llm_cost_usd_total", {"model": SONNET, "repo": "acme/calc"})
    assert cost == pytest.approx(float(COST))
    # The ratio is cache_read / (input + cache_read): 500 / (1,000 + 500).
    assert reading("longhaul_cache_ratio_count") == 1
    assert reading("longhaul_cache_ratio_sum") == pytest.approx(500 / 1_500)


async def test_a_provider_error_is_counted_by_kind_and_is_not_a_turn(counters: Metrics) -> None:
    rig = Rig(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))
    with pytest.raises(ApplicationError):
        await rig.run()

    reading = counters.registry.get_sample_value
    assert reading("longhaul_llm_errors_total", {"kind": "overloaded"}) == 1
    assert reading("longhaul_turns_total", {"role": "coder"}) == 0
    assert reading("longhaul_cache_ratio_count") == 0


async def test_a_call_that_was_billed_and_failed_still_adds_its_cost(counters: Metrics) -> None:
    billed = Usage(input_tokens=700, output_tokens=30)
    rig = Rig(ProviderError(ProviderErrorKind.SERVER, "body did not parse", usage=billed))
    with pytest.raises(ApplicationError):
        await rig.run()

    reading = counters.registry.get_sample_value
    assert reading("longhaul_llm_errors_total", {"kind": "server"}) == 1
    cost = reading("longhaul_llm_cost_usd_total", {"model": SONNET, "repo": "acme/calc"})
    assert cost == pytest.approx(0.0017)


async def test_a_retry_that_succeeds_counts_one_error_and_one_turn(counters: Metrics) -> None:
    rig = Rig(
        ProviderError(ProviderErrorKind.RATE_LIMIT, "slow down"),
        reply("end_turn", TextBlock(text="Done.")),
    )
    with pytest.raises(ApplicationError):
        await rig.run(attempt=1)
    await rig.run(attempt=2)

    reading = counters.registry.get_sample_value
    assert reading("longhaul_llm_errors_total", {"kind": "rate_limit"}) == 1
    assert reading("longhaul_turns_total", {"role": "coder"}) == 1


async def test_a_worker_without_a_ledger_counts_the_turn_and_no_cost(counters: Metrics) -> None:
    rig = Rig(reply("end_turn", TextBlock(text="Done.")))
    bare = LlmActivities(rig.provider, MemoryBlobStore(), rig.recorder, TOOLS, lambda role: CFG)
    inp = acts.LlmTurnInput(
        task_id=SPEC.task_id, turn_index=0, role=Role.CODER, spec=SPEC, transcript_ref=None
    )
    await ActivityEnvironment().run(bare.llm_turn, inp)

    reading = counters.registry.get_sample_value
    assert reading("longhaul_turns_total", {"role": "coder"}) == 1
    assert reading("longhaul_llm_cost_usd_total", {"model": SONNET, "repo": "acme/calc"}) in (
        None,
        0,
    )
