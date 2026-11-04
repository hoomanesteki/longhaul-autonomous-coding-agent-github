import pytest
from calc import divide


def test_divide_keeps_the_fraction() -> None:
    assert divide(7, 2) == pytest.approx(3.5)
    assert divide(-7, 2) == pytest.approx(-3.5)


def test_divide_evenly_still_works() -> None:
    assert divide(8, 2) == 4


def test_divide_by_zero_still_raises() -> None:
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)
