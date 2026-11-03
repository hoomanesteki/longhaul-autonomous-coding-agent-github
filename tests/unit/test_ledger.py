"""The ledger objects that need no database: the in-memory ledger and the one that does nothing.

tests/integration/test_cost_ledger.py runs the SQL ledger against Postgres. The in-memory ledger
stands in for it in the unit tests of the activities, so it must price and round the same way.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from longhaul.contracts import Usage
from longhaul.cost.ledger import Ledger, LedgerEntry, MemoryLedger, NoLedger
from longhaul.cost.pricing import Pricing, PricingError

pytestmark = [pytest.mark.unit, pytest.mark.phase7]

PRICING = Path(__file__).resolve().parents[2] / "config" / "pricing.yaml"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"


def memory() -> MemoryLedger:
    return MemoryLedger(Pricing.load(PRICING))


async def test_the_memory_ledger_keeps_the_call_and_returns_its_cost() -> None:
    ledger = memory()
    usage = Usage(input_tokens=336, output_tokens=482, cache_write_tokens=6504)

    cost = await ledger.record("lh-acme-calc-42", "acme/calc", SONNET, 3, 2, usage)

    # 336 * 2.00 + 482 * 10.00 + 6504 * 2.50 = 21752 per million tokens.
    assert cost == Decimal("0.021752")
    assert ledger.entries == [
        LedgerEntry("lh-acme-calc-42", "acme/calc", SONNET, 3, 2, usage, Decimal("0.021752"))
    ]


async def test_every_call_is_a_row_also_a_second_one_for_the_same_turn_and_attempt() -> None:
    # A reply cut off at max_tokens is asked again by a new activity run, which starts at
    # attempt 1 again, so the same turn and attempt come back with another call.
    ledger = memory()
    for _ in range(2):
        await ledger.record("lh-x", "acme/calc", SONNET, 0, 1, Usage(input_tokens=10))
    assert len(ledger.entries) == 2


async def test_the_cost_is_rounded_to_the_millionth_of_a_dollar_that_the_column_holds() -> None:
    ledger = memory()
    # Haiku writes the cache at 0.125 dollars per million tokens: 3 tokens are 0.000000375.
    assert await ledger.record("t", "r", HAIKU, 0, 1, Usage(cache_write_tokens=3)) == Decimal(
        "0.000000"
    )
    assert await ledger.record("t", "r", HAIKU, 0, 1, Usage(cache_write_tokens=5)) == Decimal(
        "0.000001"
    )
    # 4 tokens are 0.0000005, exactly half. Half rounds up, as Postgres does.
    assert await ledger.record("t", "r", HAIKU, 0, 1, Usage(cache_write_tokens=4)) == Decimal(
        "0.000001"
    )


async def test_a_model_without_a_price_is_refused_and_leaves_no_row() -> None:
    ledger = memory()
    with pytest.raises(PricingError, match="claude-opus-9"):
        await ledger.record("t", "r", "claude-opus-9", 0, 1, Usage(input_tokens=5))
    assert ledger.entries == []


async def test_a_ledger_on_free_pricing_keeps_its_rows_at_no_cost() -> None:
    ledger = MemoryLedger(Pricing.free())
    cost = await ledger.record("t", "r", "scripted-model", 0, 1, Usage(input_tokens=5_000))
    assert cost == 0
    assert [e.usage.input_tokens for e in ledger.entries] == [5_000]


async def test_the_ledger_that_does_nothing_costs_nothing() -> None:
    cost = await NoLedger().record("t", "r", SONNET, 0, 1, Usage(input_tokens=1_000_000))
    assert cost == 0


def test_the_ledgers_without_a_database_fit_the_protocol() -> None:
    ledgers: list[Ledger] = [memory(), NoLedger()]
    assert len(ledgers) == 2
