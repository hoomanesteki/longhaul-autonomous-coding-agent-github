def truncate(text: str, width: int, suffix: str = "...") -> str:
    """Cut text down to width characters, ending with suffix when something was cut."""
    if len(text) <= width:
        return text
    return text[:width] + suffix
