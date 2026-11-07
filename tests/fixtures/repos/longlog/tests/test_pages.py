from longlog.pages import page_count


def test_page_count_rounds_up() -> None:
    assert page_count(21, 10) == 3
