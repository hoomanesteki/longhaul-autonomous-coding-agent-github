from longlog.ranges import inclusive_range


def test_inclusive_range_includes_the_stop() -> None:
    assert inclusive_range(3, 6) == [3, 4, 5, 6]
