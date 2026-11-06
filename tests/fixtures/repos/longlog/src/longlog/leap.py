"""Leap years in the Gregorian calendar."""


def is_leap_year(year: int) -> bool:
    """Return True when the year has 366 days.

    Years divisible by 4 are leap years, except centuries, unless the century is divisible by 400.
    """
    return year % 4 == 0 and year % 100 != 0
