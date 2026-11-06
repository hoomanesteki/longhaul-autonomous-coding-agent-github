import pytest
from calc import percent_change


def test_an_increase_is_positive() -> None:
    assert percent_change(50, 75) == pytest.approx(50.0)


def test_a_decrease_is_negative() -> None:
    assert percent_change(80, 60) == pytest.approx(-25.0)


def test_no_change_is_zero() -> None:
    assert percent_change(10, 10) == pytest.approx(0.0)


def test_a_starting_value_of_zero_raises() -> None:
    with pytest.raises(ValueError):
        percent_change(0, 5)
