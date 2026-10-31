import pytest
from inventory import Inventory


def test_add_increases_quantity() -> None:
    inv = Inventory()
    inv.add("widget", 3)
    inv.add("widget", 2)
    assert inv.quantity("widget") == 5


def test_remove_decreases_quantity() -> None:
    inv = Inventory()
    inv.add("widget", 5)
    inv.remove("widget", 2)
    assert inv.quantity("widget") == 3


def test_unknown_sku_has_zero_quantity() -> None:
    assert Inventory().quantity("gadget") == 0


def test_add_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError):
        Inventory().add("widget", 0)


def test_remove_more_than_stock_is_rejected() -> None:
    inv = Inventory()
    inv.add("widget", 2)
    with pytest.raises(ValueError, match="widget"):
        inv.remove("widget", 5)
    assert inv.quantity("widget") == 2
