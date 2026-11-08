import time

import pytest
from inventory import Inventory, Reservations


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop every clock, so that two calls in a row see the same time."""
    monkeypatch.setattr(time, "time", lambda: 1_700_000_000.0)
    monkeypatch.setattr(time, "time_ns", lambda: 1_700_000_000_000_000_000)
    monkeypatch.setattr(time, "monotonic", lambda: 5_000.0)
    monkeypatch.setattr(time, "monotonic_ns", lambda: 5_000_000_000_000)


def stocked() -> Inventory:
    inv = Inventory()
    inv.add("widget", 10)
    return inv


def test_reservations_made_at_the_same_moment_get_different_ids(frozen_clock: None) -> None:
    holds = Reservations(stocked())
    ids = [holds.reserve("widget", 1, ttl_s=60) for _ in range(3)]
    assert len(set(ids)) == 3


def test_every_hold_is_kept(frozen_clock: None) -> None:
    holds = Reservations(stocked())
    for quantity in (1, 2, 3):
        holds.reserve("widget", quantity, ttl_s=60)
    assert holds.available("widget") == 4


def test_releasing_one_hold_frees_only_its_own_stock(frozen_clock: None) -> None:
    holds = Reservations(stocked())
    first = holds.reserve("widget", 1, ttl_s=60)
    holds.reserve("widget", 2, ttl_s=60)
    holds.release(first)
    assert holds.available("widget") == 8
