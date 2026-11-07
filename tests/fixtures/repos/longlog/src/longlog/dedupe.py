"""Remove repeated items."""


def dedupe(items: list[int]) -> list[int]:
    """Return the items without repeats, in the order they first appear."""
    return [item for i, item in enumerate(items) if item not in items[i + 1 :]]
