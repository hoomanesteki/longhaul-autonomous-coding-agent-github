"""English ordinal numbers."""


def ordinal(number: int) -> str:
    """Return the number with its suffix: 1st, 2nd, 3rd, 4th, 11th, 22nd."""
    suffixes = {1: "st", 2: "nd", 3: "rd"}
    return f"{number}{suffixes.get(number % 10, 'th')}"
