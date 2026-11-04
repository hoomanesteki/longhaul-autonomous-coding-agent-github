from inventory import Inventory, Reservations


class FakeClock:
    """A clock that only moves when the test says so."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def stocked() -> Inventory:
    inv = Inventory()
    inv.add("widget", 5)
    return inv


def test_a_hold_is_active_until_its_time_is_up() -> None:
    clock = FakeClock()
    holds = Reservations(stocked(), clock=clock)
    holds.reserve("widget", 2, ttl_s=10)
    clock.now += 9.9
    assert holds.available("widget") == 3


def test_a_hold_expires_when_the_clock_reaches_the_ttl() -> None:
    clock = FakeClock()
    holds = Reservations(stocked(), clock=clock)
    holds.reserve("widget", 2, ttl_s=10)
    clock.now += 10
    assert holds.available("widget") == 5


def test_releasing_an_expired_hold_is_harmless() -> None:
    clock = FakeClock()
    holds = Reservations(stocked(), clock=clock)
    reservation_id = holds.reserve("widget", 2, ttl_s=10)
    clock.now += 60
    holds.release(reservation_id)
    assert holds.available("widget") == 5


def test_the_default_clock_still_works() -> None:
    holds = Reservations(stocked())
    holds.reserve("widget", 2, ttl_s=60)
    assert holds.available("widget") == 3
