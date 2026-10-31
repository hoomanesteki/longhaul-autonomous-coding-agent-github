"""Invariant tests. Each test names the invariant it pins."""

import pickle

import pytest
from pydantic import BaseModel

from longhaul.contracts import BlobRef
from longhaul.durable.payload_guard import (
    DEFAULT_LIMIT,
    PayloadTooLargeError,
    assert_small,
    json_size,
)

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


class Note(BaseModel):
    text: str


def _string_of_json_size(size: int) -> str:
    """A string whose JSON form is exactly `size` bytes: the text plus two quotes."""
    return "x" * (size - 2)


def test_payload_size_guard() -> None:
    # I2: nothing larger than 256 KB crosses an activity boundary.
    with pytest.raises(PayloadTooLargeError) as caught:
        assert_small({"transcript": "x" * 300_000})
    assert caught.value.limit == 256_000
    assert caught.value.size > 300_000
    assert "blob store" in str(caught.value)

    assert_small({"transcript": "x" * 1_000})


def test_payload_size_guard_measures_pydantic_models() -> None:
    assert_small(Note(text="short"))
    with pytest.raises(PayloadTooLargeError):
        assert_small(Note(text="x" * 300_000))


def test_payload_size_guard_measures_lists_of_models() -> None:
    # A transcript is a list of messages, which is how the largest payloads look.
    assert_small([Note(text="x" * 100)] * 10)
    with pytest.raises(PayloadTooLargeError):
        assert_small([Note(text="x" * 100_000)] * 3)


def test_payload_size_guard_limit_is_inclusive() -> None:
    at_limit = _string_of_json_size(DEFAULT_LIMIT)
    assert json_size(at_limit) == DEFAULT_LIMIT
    assert_small(at_limit)

    with pytest.raises(PayloadTooLargeError) as caught:
        assert_small(at_limit + "x")
    assert caught.value.size == DEFAULT_LIMIT + 1


def test_payload_size_guard_takes_its_own_limit() -> None:
    assert_small("x" * 100, limit=1_000)
    with pytest.raises(PayloadTooLargeError) as caught:
        assert_small("x" * 100, limit=50)
    assert caught.value.limit == 50
    assert caught.value.size == 102


def test_payload_size_guard_counts_utf8_bytes_not_characters() -> None:
    # Three bytes per character: 100 characters are 300 bytes plus the quotes.
    assert json_size("€" * 100) == 302


def test_payload_too_large_error_survives_pickling() -> None:
    # Exceptions cross process boundaries by pickle, which replays only the message
    # unless the class says how to rebuild itself.
    error = pickle.loads(pickle.dumps(PayloadTooLargeError(300_002, 256_000)))
    assert isinstance(error, PayloadTooLargeError)
    assert (error.size, error.limit) == (300_002, 256_000)
    assert str(error) == str(PayloadTooLargeError(300_002, 256_000))


def test_a_blob_ref_is_tiny_next_to_the_limit() -> None:
    # The point of ADR-0005: a 5 MB payload crosses the boundary as this.
    ref = BlobRef(key="tasks/lh-o-r-42/turns/0007/response.json", sha256="0" * 64, size=5_000_000)
    assert json_size(ref) < 200
