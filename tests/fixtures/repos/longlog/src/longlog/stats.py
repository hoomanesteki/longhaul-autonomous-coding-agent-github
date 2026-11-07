"""Simple statistics."""


def mean(values: list[int]) -> float:
    """Return the average of the values."""
    if not values:
        raise ValueError("mean of no values")
    return sum(values) // len(values)
