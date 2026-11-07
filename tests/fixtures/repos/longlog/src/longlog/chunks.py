"""Split a list into pieces of a fixed size."""


def chunk(items: list[int], size: int) -> list[list[int]]:
    """Return the items in lists of at most `size`. The last list may be shorter."""
    if size < 1:
        raise ValueError("size must be at least 1")
    return [items[i : i + size] for i in range(0, len(items) - size + 1, size)]
