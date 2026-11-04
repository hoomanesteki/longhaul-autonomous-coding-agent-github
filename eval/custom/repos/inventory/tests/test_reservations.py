import time

import pytest
from inventory import Inventory, Reservations


def stocked(quantity: int = 5) -> Inventory:
    inv = Inventory()
    inv.add("widget", quantity)
    return inv


def test_reserving_reduces_what_is_available() -> None:
    holds = Reservations(stocked())
    holds.reserve("widget", 2, ttl_s=60)
    assert holds.available("widget") == 3


def test_releasing_gives_the_stock_back() -> None:
    holds = Reservations(stocked())
    reservation_id = holds.reserve("widget", 2, ttl_s=60)
    holds.release(reservation_id)
    assert holds.available("widget") == 5


def test_cannot_reserve_more_than_is_available() -> None:
    holds = Reservations(stocked(2))
    with pytest.raises(ValueError, match="widget"):
        holds.reserve("widget", 3, ttl_s=60)


def test_an_expired_hold_frees_its_stock() -> None:
    holds = Reservations(stocked())
    holds.reserve("widget", 2, ttl_s=0.01)
    time.sleep(0.3)
    assert holds.available("widget") == 5
