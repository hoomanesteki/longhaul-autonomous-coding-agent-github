"""The size of a hint (LH-506).

A hint is kept for the whole task and goes to every `llm_turn`, so a few big ones would push the
input of that activity over the payload guard (LH-292). Each hint is cut when the signal arrives.
"""

import pytest

from longhaul.agent import hints

pytestmark = [pytest.mark.unit, pytest.mark.phase5]


def test_a_short_hint_is_kept_as_it_is() -> None:
    assert hints.clip_hint("try pytest -x") == "try pytest -x"


def test_a_long_hint_is_cut_and_says_so() -> None:
    text = "a" * (hints.MAX_HINT_BYTES * 3)
    got = hints.clip_hint(text)
    assert got.startswith("a" * hints.MAX_HINT_BYTES)
    assert got.endswith(hints.CUT_NOTE)
    assert len(got.encode()) <= hints.MAX_HINT_BYTES + len(hints.CUT_NOTE.encode())


def test_a_hint_at_the_limit_is_not_cut() -> None:
    text = "b" * hints.MAX_HINT_BYTES
    assert hints.clip_hint(text) == text


def test_the_cut_never_splits_a_character() -> None:
    text = "é" * hints.MAX_HINT_BYTES  # two bytes each
    got = hints.clip_hint(text)
    assert got.endswith(hints.CUT_NOTE)
    got.encode()  # raises on a broken character


def test_the_name_of_the_author_is_cut_too() -> None:
    assert len(hints.clip_author("x" * 500)) <= hints.MAX_AUTHOR_CHARS
    assert hints.clip_author("alice") == "alice"
