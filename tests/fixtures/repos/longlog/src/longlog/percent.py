"""Percentages."""


def percent(part: float, whole: float) -> float:
    """Return `part` as a percentage of `whole`, rounded to one decimal place."""
    if whole == 0:
        raise ValueError("whole must not be zero")
    return round(whole / part * 100, 1)
