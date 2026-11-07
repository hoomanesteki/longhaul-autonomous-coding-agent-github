from longlog.dedupe import dedupe


def test_dedupe_keeps_the_first_copy() -> None:
    assert dedupe([3, 1, 3, 2, 1]) == [3, 1, 2]
