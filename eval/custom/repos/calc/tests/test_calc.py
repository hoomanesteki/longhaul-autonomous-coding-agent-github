import pytest
from calc import add, divide, multiply


def test_add_with_zero() -> None:
    assert add(7, 0) == 7


def test_multiply() -> None:
    assert multiply(4, 3) == 12


def test_divide_evenly() -> None:
    assert divide(8, 2) == 4


def test_divide_by_zero_raises() -> None:
    with pytest.raises(ZeroDivisionError):
        divide(1, 0)
