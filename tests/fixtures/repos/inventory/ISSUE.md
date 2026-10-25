# remove() allows negative stock

`Inventory.remove()` does not check how much is on hand. Removing more than
is in stock leaves a negative count.

```python
from inventory import Inventory

inv = Inventory()
inv.add("widget", 2)
inv.remove("widget", 5)
inv.quantity("widget")  # returns -3, should be rejected
```

Expected: `remove()` raises a `ValueError` and leaves the stock unchanged. Please
also add an `InventoryError` (a subclass of `ValueError`) so callers can catch
inventory problems on their own.

Repro: run `python -m pytest -q`. The test `tests/test_store.py::test_remove_more_than_stock_is_rejected` fails. The other tests should keep passing.
