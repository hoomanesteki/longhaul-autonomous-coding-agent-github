from longlog.ordinal import ordinal


def test_the_teens_end_in_th() -> None:
    assert [ordinal(n) for n in (11, 12, 13)] == ["11th", "12th", "13th"]
