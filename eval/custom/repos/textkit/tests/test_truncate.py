from textkit import truncate


def test_short_text_is_kept() -> None:
    assert truncate("short", 10) == "short"


def test_text_of_exactly_the_width_is_kept() -> None:
    assert truncate("exactly10!", 10) == "exactly10!"
