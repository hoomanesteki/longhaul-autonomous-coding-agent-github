import pytest
from calc import add


def test_add_returns_the_sum() -> None:
    assert add(2, 3) == 5


def test_add_handles_negative_numbers() -> None:
    assert add(-4, 4) == 0
    assert add(-2, -3) == -5


def test_add_handles_fractions() -> None:
    assert add(0.5, 0.25) == pytest.approx(0.75)
