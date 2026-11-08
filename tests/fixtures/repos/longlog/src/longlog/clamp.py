"""Keep a number inside a range."""


def clamp(value: float, low: float, high: float) -> float:
    """Return value limited to the closed range from low to high."""
    if low > high:
        raise ValueError("low must not be above high")
    return max(low, min(value, high - 1))
