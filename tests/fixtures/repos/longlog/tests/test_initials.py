from longlog.initials import initials


def test_initials_uses_every_word() -> None:
    assert initials("ada king lovelace") == "AKL"
