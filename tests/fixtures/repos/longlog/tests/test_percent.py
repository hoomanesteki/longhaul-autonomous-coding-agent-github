from longlog.percent import percent


def test_percent_of_a_whole() -> None:
    assert percent(1, 8) == 12.5
