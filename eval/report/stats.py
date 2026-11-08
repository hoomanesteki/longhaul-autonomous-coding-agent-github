"""The statistics of the results report, with no dependency beyond the standard library (LH-905).

Two functions carry the claims of the report. `wilson` puts an interval around a resolve rate,
and `mcnemar_exact` says whether two configs that ran the same tasks differ by more than chance.
scipy is not a dependency of this project, and both are short enough to check by hand, which
tests/unit/test_stats.py does.

Why these two. A resolve rate on 15 or 40 tasks is a rough number. The normal-approximation
interval is wrong at the edges (it leaves [0, 1] near 0% and 100%) and for small n, so the report
uses the Wilson score interval. The two configs are compared on the same tasks, so the test has
to be paired: only tasks that one config solved and the other did not carry information.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence

# The two-sided 95% point of the standard normal distribution.
Z_95 = 1.959963984540054


def wilson(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval for `successes` out of `n`, as (low, high) in [0, 1]."""
    if n <= 0 or not 0 <= successes <= n:
        raise ValueError(f"successes must be between 0 and n, and n above 0: {successes} of {n}")
    share = successes / n
    scale = 1 + z * z / n
    centre = (share + z * z / (2 * n)) / scale
    spread = z * math.sqrt(share * (1 - share) / n + z * z / (4 * n * n)) / scale
    # Rounding can leave 0.9999999999999999 where the interval ends at 1.
    return max(0.0, centre - spread), min(1.0, centre + spread)


def mcnemar_exact(b: int, c: int) -> float:
    """The two-sided p of McNemar's test, from the exact binomial, for discordant pairs b and c.

    b is the number of tasks only the first config solved, c the number only the second solved.
    Under "no difference" each discordant pair falls either way with probability 1/2, so the
    smaller count follows Binomial(b + c, 1/2) and p is twice its lower tail, capped at 1. The sum
    is done with integers, because 2**n passes the largest float for n above 1023. The
    continuity-corrected chi-square is not used: it is an approximation to this, and the exact
    form is cheap at every size a task set reaches.
    """
    if b < 0 or c < 0:
        raise ValueError(f"a discordant count cannot be negative: b={b}, c={c}")
    total = b + c
    if total == 0:
        return 1.0
    tail = sum(math.comb(total, k) for k in range(min(b, c) + 1))
    return min(1.0, 2 * tail / 2**total)


def median_iqr(values: Sequence[float]) -> tuple[float, float, float]:
    """(median, first quartile, third quartile). One value gives it three times."""
    if not values:
        raise ValueError("no values")
    if len(values) == 1:
        return values[0], values[0], values[0]
    low, _, high = statistics.quantiles(values, n=4, method="inclusive")
    return statistics.median(values), low, high
