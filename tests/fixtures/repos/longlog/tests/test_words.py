from longlog.words import word_count


def test_word_count_ignores_extra_spaces() -> None:
    assert word_count("one  two   three") == 3
