from longlog.clamp import clamp


def test_clamp_allows_the_upper_bound() -> None:
    assert clamp(15, 0, 10) == 10
