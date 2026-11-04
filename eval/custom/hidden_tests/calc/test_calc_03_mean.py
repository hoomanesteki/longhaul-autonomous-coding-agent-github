import pytest
from calc import mean


def test_mean_of_a_list() -> None:
    assert mean([1, 2, 3, 4]) == pytest.approx(2.5)


def test_mean_of_a_tuple_with_one_item_is_a_float() -> None:
    result = mean((10,))
    assert result == 10.0
    assert isinstance(result, float)


def test_mean_of_mixed_signs() -> None:
    assert mean([-2, 2, 6]) == pytest.approx(2.0)


def test_mean_of_an_empty_list_raises() -> None:
    with pytest.raises(ValueError):
        mean([])
