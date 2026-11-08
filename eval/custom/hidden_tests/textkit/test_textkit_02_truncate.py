from textkit import truncate


def test_the_result_is_never_longer_than_the_width() -> None:
    assert truncate("hello world", 8) == "hello..."
    assert len(truncate("x" * 50, 20)) == 20


def test_a_custom_suffix_counts_toward_the_width() -> None:
    assert truncate("hello world", 8, suffix="~") == "hello w~"


def test_text_that_fits_is_returned_as_it_is() -> None:
    assert truncate("short", 10) == "short"
    assert truncate("exactly10!", 10) == "exactly10!"
