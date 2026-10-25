from textkit import slugify


def test_slugify_basic() -> None:
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_collapses_separators() -> None:
    assert slugify("  a -- b  ") == "a-b"


def test_slugify_accented_latin() -> None:
    assert slugify("Crème Brûlée") == "creme-brulee"
