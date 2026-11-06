# inventory

In-memory stock levels per SKU, with short holds for orders that are being paid.

```python
from inventory import Inventory, Reservations

inv = Inventory()
inv.add("widget", 3)
inv.remove("widget", 1)
inv.quantity("widget")  # 2

holds = Reservations(inv)
reservation_id = holds.reserve("widget", 1, ttl_s=300)
holds.available("widget")  # 1
holds.release(reservation_id)
```

`dump_json(inv)` and `dump_yaml(inv)` write the stock levels out as text.

Run the tests with `python -m pytest -q`.
