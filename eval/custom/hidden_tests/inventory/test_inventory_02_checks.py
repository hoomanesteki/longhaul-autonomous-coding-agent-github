from pathlib import Path

import inventory
import pytest
from inventory import Inventory, Reservations
from inventory.checks import validate_quantity

MESSAGE = "quantity must be positive"


def source_dir() -> Path:
    return Path(inventory.__file__).resolve().parent


def test_validate_quantity_returns_a_good_quantity() -> None:
    assert validate_quantity(3) == 3


@pytest.mark.parametrize("quantity", [0, -1])
def test_validate_quantity_rejects_a_quantity_that_is_not_positive(quantity: int) -> None:
    with pytest.raises(ValueError, match=MESSAGE):
        validate_quantity(quantity)


def test_add_remove_and_reserve_still_reject_a_quantity_that_is_not_positive() -> None:
    inv = Inventory()
    inv.add("widget", 5)
    with pytest.raises(ValueError, match=MESSAGE):
        inv.add("widget", 0)
    with pytest.raises(ValueError, match=MESSAGE):
        inv.remove("widget", -1)
    with pytest.raises(ValueError, match=MESSAGE):
        Reservations(inv).reserve("widget", 0, ttl_s=60)


def test_the_message_is_written_in_one_place() -> None:
    lines = [
        f"{path.name}: {line.strip()}"
        for path in sorted(source_dir().rglob("*.py"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if MESSAGE in line
    ]
    assert len(lines) == 1, lines
