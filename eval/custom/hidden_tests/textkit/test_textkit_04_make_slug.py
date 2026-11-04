import warnings

import pytest
from textkit import make_slug, slugify


def test_make_slug_builds_the_slug() -> None:
    assert make_slug("Hello, World!") == "hello-world"
    assert make_slug("  a -- b  ") == "a-b"


def test_make_slug_does_not_warn() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        make_slug("Hello, World!")


def test_slugify_still_works_but_warns() -> None:
    with pytest.warns(DeprecationWarning):
        assert slugify("Hello, World!") == "hello-world"
