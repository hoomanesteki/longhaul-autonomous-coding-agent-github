class Inventory:
    """Tracks the on-hand quantity of each SKU."""

    def __init__(self) -> None:
        self._stock: dict[str, int] = {}

    def add(self, sku: str, quantity: int) -> None:
        """Increase the stock of sku by quantity."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._stock[sku] = self.quantity(sku) + quantity

    def remove(self, sku: str, quantity: int) -> None:
        """Decrease the stock of sku by quantity."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        self._stock[sku] = self.quantity(sku) - quantity

    def quantity(self, sku: str) -> int:
        """Return the on-hand quantity of sku, or 0 if it is unknown."""
        return self._stock.get(sku, 0)
