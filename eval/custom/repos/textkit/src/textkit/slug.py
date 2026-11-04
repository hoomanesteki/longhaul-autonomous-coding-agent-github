import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Turn text into a lowercase, dash-separated slug."""
    return _NON_ALNUM.sub("-", text.lower()).strip("-")
