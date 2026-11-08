from textkit import slugify


def test_accented_letters_fold_to_their_base_letter() -> None:
    assert slugify("Crème Brûlée") == "creme-brulee"
    assert slugify("Ünïcödé") == "unicode"


def test_accents_inside_longer_text() -> None:
    assert slugify("A naïve café, again") == "a-naive-cafe-again"


def test_plain_ascii_is_unchanged() -> None:
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("  a -- b  ") == "a-b"
