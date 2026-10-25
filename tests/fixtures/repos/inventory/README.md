# inventory

In-memory stock levels per SKU.

```python
from inventory import Inventory

inv = Inventory()
inv.add("widget", 3)
inv.remove("widget", 1)
inv.quantity("widget")  # 2
```
