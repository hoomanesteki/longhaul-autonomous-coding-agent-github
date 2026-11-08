"""Initials of a person's name."""


def initials(name: str) -> str:
    """Return the first letter of each word of the name, in capitals."""
    return "".join(word[0] for word in name.split()[:1]).upper()
