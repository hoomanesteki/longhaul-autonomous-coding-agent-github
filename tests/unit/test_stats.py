"""The two statistics of the results report (LH-905): Wilson intervals and McNemar's exact test.

The reference values were worked out by hand from the formulas, not taken from the code under
test. The first Wilson row is the usual textbook example (8 of 10 gives 0.490 to 0.943). The
McNemar values are sums of binomial coefficients: for b=2, c=8 the tail is
(1 + 10 + 45) / 1024, and the two-sided p is twice that.
"""

import pytest

from eval.report import stats

pytestmark = [pytest.mark.unit, pytest.mark.phase9]


@pytest.mark.parametrize(
    ("successes", "n", "low", "high"),
    [
        (8, 10, 0.49016, 0.94332),
        (0, 10, 0.0, 0.27753),
        (10, 10, 0.72247, 1.0),
        (27, 40, 0.52018, 0.79915),
        (5, 15, 0.15176, 0.58286),
        (1, 1, 0.20655, 1.0),
    ],
)
def test_wilson(successes: int, n: int, low: float, high: float) -> None:
    got_low, got_high = stats.wilson(successes, n)
    assert got_low == pytest.approx(low, abs=5e-6)
    assert got_high == pytest.approx(high, abs=5e-6)


def test_wilson_stays_inside_zero_and_one() -> None:
    # The normal approximation leaves [0, 1] at the edges. Wilson must not.
    for successes in (0, 1, 19, 20):
        low, high = stats.wilson(successes, 20)
        assert 0.0 <= low <= successes / 20 <= high <= 1.0


def test_wilson_interval_narrows_as_the_sample_grows() -> None:
    small = stats.wilson(15, 20)
    large = stats.wilson(150, 200)
    assert (large[1] - large[0]) < (small[1] - small[0])


@pytest.mark.parametrize(("successes", "n"), [(0, 0), (-1, 5), (6, 5)])
def test_wilson_rejects_impossible_counts(successes: int, n: int) -> None:
    with pytest.raises(ValueError, match="successes"):
        stats.wilson(successes, n)


@pytest.mark.parametrize(
    ("b", "c", "p"),
    [
        (2, 8, 0.109375),  # 2 * 56 / 1024
        (8, 2, 0.109375),  # the test does not care which side wins
        (0, 5, 0.0625),  # 2 * 1 / 32
        (1, 11, 0.00634765625),  # 2 * 13 / 4096
        (10, 1, 0.01171875),  # 2 * 12 / 2048
        (3, 3, 1.0),  # a tie is capped at 1, not 1.2
        (0, 0, 1.0),  # no discordant pair, nothing to test
    ],
)
def test_mcnemar(b: int, c: int, p: float) -> None:
    assert stats.mcnemar_exact(b, c) == pytest.approx(p, abs=1e-12)


def test_mcnemar_is_exact_for_large_counts() -> None:
    # 2**1200 is far past the largest float, so a binomial computed in floats overflows here.
    assert 0.0 < stats.mcnemar_exact(400, 800) < 1e-20


def test_mcnemar_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="count"):
        stats.mcnemar_exact(-1, 3)


def test_median_and_quartiles() -> None:
    assert stats.median_iqr([1.0, 2.0, 3.0, 4.0, 5.0]) == (3.0, 2.0, 4.0)
    assert stats.median_iqr([7.0]) == (7.0, 7.0, 7.0)
