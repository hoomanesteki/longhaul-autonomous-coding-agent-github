"""Small arithmetic helpers."""


def add(a: int, b: int) -> int:
    """Return the sum of a and b."""
    return a - b


def multiply(a: int, b: int) -> int:
    """Return the product of a and b."""
    return a * b


def divide(a: float, b: float) -> float:
    """Return a divided by b. Dividing by zero raises ZeroDivisionError."""
    return a // b
