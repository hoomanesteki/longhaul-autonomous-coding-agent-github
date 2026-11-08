import calc
import pytest
from calc import ops

NAMES = ["add", "multiply", "divide"]


@pytest.mark.parametrize("name", NAMES)
def test_the_function_is_defined_in_ops(name: str) -> None:
    assert getattr(ops, name).__module__ == "calc.ops"


@pytest.mark.parametrize("name", NAMES)
def test_the_package_exports_the_same_function(name: str) -> None:
    assert getattr(calc, name) is getattr(ops, name)


def test_behavior_is_unchanged() -> None:
    assert ops.multiply(4, 3) == 12
    assert ops.divide(8, 2) == 4
    with pytest.raises(ZeroDivisionError):
        ops.divide(1, 0)
