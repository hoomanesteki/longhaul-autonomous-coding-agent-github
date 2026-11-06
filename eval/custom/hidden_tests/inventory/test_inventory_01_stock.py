import pytest
from inventory import Inventory, InventoryError


def test_inventory_error_is_a_value_error() -> None:
    assert issubclass(InventoryError, ValueError)


def test_removing_more_than_is_in_stock_raises() -> None:
    inv = Inventory()
    inv.add("widget", 2)
    with pytest.raises(InventoryError, match="widget"):
        inv.remove("widget", 5)


def test_a_rejected_removal_leaves_the_stock_unchanged() -> None:
    inv = Inventory()
    inv.add("widget", 2)
    with pytest.raises(ValueError):
        inv.remove("widget", 5)
    assert inv.quantity("widget") == 2


def test_removing_an_unknown_sku_raises() -> None:
    with pytest.raises(InventoryError):
        Inventory().remove("gadget", 1)


def test_removing_exactly_what_is_in_stock_is_allowed() -> None:
    inv = Inventory()
    inv.add("widget", 2)
    inv.remove("widget", 2)
    assert inv.quantity("widget") == 0
