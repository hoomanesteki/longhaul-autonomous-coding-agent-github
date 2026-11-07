"""Counting words."""


def word_count(text: str) -> int:
    """Return how many words the text has. White space of any length separates them."""
    return len(text.split(" "))
