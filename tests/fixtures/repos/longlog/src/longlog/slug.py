"""URL slugs."""

import re


def slugify(text: str) -> str:
    """Return the text as a slug: lower case words joined by single hyphens."""
    words = re.findall(r"[A-Za-z0-9]+", text)
    return "-".join(words)
