from longlog.leap import is_leap_year


def test_a_century_divisible_by_400_is_a_leap_year() -> None:
    assert is_leap_year(2000)
