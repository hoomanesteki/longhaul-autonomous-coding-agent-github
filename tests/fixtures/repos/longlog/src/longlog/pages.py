"""Paging through a long list."""


def page_count(total: int, per_page: int) -> int:
    """Return how many pages show `total` items, `per_page` on each."""
    if per_page < 1:
        raise ValueError("per_page must be at least 1")
    return total // per_page
