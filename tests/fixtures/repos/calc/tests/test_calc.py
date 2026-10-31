from calc import add, multiply


def test_add_positive_numbers() -> None:
    assert add(2, 3) == 5


def test_add_with_zero() -> None:
    assert add(7, 0) == 7


def test_multiply() -> None:
    assert multiply(4, 3) == 12
