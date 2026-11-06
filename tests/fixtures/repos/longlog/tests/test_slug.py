from longlog.slug import slugify


def test_slugify_lowercases() -> None:
    assert slugify("Hello, World!") == "hello-world"
