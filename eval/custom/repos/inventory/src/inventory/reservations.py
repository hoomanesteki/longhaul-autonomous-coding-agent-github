"""Short holds on stock, for orders that are still being paid."""

import time
from dataclasses import dataclass

from inventory.store import Inventory


@dataclass
class _Hold:
    sku: str
    quantity: int
    expires_at: float


class Reservations:
    """Reserve stock for a number of seconds. An expired hold frees its stock again."""

    def __init__(self, inventory: Inventory) -> None:
        self._inventory = inventory
        self._holds: dict[str, _Hold] = {}

    def reserve(self, sku: str, quantity: int, ttl_s: float) -> str:
        """Hold quantity of sku for ttl_s seconds and return the reservation id."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        free = self.available(sku)
        if quantity > free:
            raise ValueError(f"{sku}: only {free} available")
        reservation_id = f"res-{int(time.time())}"
        self._holds[reservation_id] = _Hold(sku, quantity, time.time() + ttl_s)
        return reservation_id

    def release(self, reservation_id: str) -> None:
        """Give a hold back. An unknown or expired id is ignored."""
        self._holds.pop(reservation_id, None)

    def available(self, sku: str) -> int:
        """Return the stock of sku that is on hand and not held."""
        self._drop_expired()
        held = sum(h.quantity for h in self._holds.values() if h.sku == sku)
        return self._inventory.quantity(sku) - held

    def _drop_expired(self) -> None:
        now = time.time()
        for reservation_id in [r for r, h in self._holds.items() if h.expires_at <= now]:
            del self._holds[reservation_id]
