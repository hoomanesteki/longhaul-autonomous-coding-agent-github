from longlog.stats import mean


def test_mean_keeps_the_fraction() -> None:
    assert mean([1, 2]) == 1.5
