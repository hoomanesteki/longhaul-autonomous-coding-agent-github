"""The parts of the memory store that need no database (LH-603).

tests/integration/test_memory_store.py runs the store itself on Postgres.
"""

from __future__ import annotations

import hashlib
import inspect
import re

import pytest
from longhaul.memory.store import (
    DEFAULT_BRANCH_APPROVER,
    MemoryItemNotFoundError,
    PgMemoryStore,
    content_hash,
    normalize_content,
)

from longhaul.contracts import MemoryStore

pytestmark = [pytest.mark.unit, pytest.mark.phase6]


def test_normalizing_lowercases_and_collapses_whitespace() -> None:
    assert (
        normalize_content("  Run\tPYTEST -q\n\n from the  root \n") == "run pytest -q from the root"
    )


def test_normalizing_keeps_the_words_and_their_order() -> None:
    assert normalize_content("b a") == "b a"
    assert normalize_content("ab") != normalize_content("a b")
    assert normalize_content("pytest -q") != normalize_content("pytest")


@pytest.mark.parametrize("text", ["", " ", "\n\t \r\n", "  "])
def test_a_blank_text_has_no_hash(text: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        content_hash(text)


def test_the_hash_ignores_case_and_spacing_only() -> None:
    plain = content_hash("Run pytest -q before finish.")
    assert plain == content_hash("  run   PYTEST -q\nbefore finish. ")
    assert plain != content_hash("Run pytest before finish.")
    assert re.fullmatch(r"[0-9a-f]{64}", plain)


def test_the_hash_is_the_same_in_every_process() -> None:
    # The rows of one day must match the notes of the next. This is sha256 of the normalized
    # text, so it cannot change with PYTHONHASHSEED or with the Python version.
    assert content_hash("Pytest -Q") == hashlib.sha256(b"pytest -q").hexdigest()


def test_the_store_has_every_method_of_the_contract() -> None:
    for name in ("search", "add_candidate", "promote", "reject"):
        ours = inspect.signature(getattr(PgMemoryStore, name))
        theirs = inspect.signature(getattr(MemoryStore, name))
        # The store may add keyword-only options, but never takes fewer positional arguments.
        assert list(ours.parameters)[: len(theirs.parameters)] == list(theirs.parameters)


def test_a_missing_item_is_a_lookup_error() -> None:
    assert issubclass(MemoryItemNotFoundError, LookupError)


def test_the_default_branch_approver_names_the_path_of_the_policy() -> None:
    # ADR-0010 lists three ways to approve an item. This is the first of them.
    assert DEFAULT_BRANCH_APPROVER == "default_branch"
