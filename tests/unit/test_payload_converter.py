"""Invariant I2 in the data converter (LH-292): no payload over 256 KB is encoded.

`assert_small` in the activities checks a payload after the workflow has scheduled it, so an
oversized one is already in the history by then. The guard in the payload converter runs where
the payload is made, on the client and in the workflow, and refuses it before it reaches the
server. These tests encode through the data converter that `connect` gives every client and
worker, the way the SDK does.
"""

from typing import Any

import pytest
from pydantic import BaseModel
from temporalio.api.common.v1 import Payload
from temporalio.common import RawValue
from temporalio.contrib.pydantic import pydantic_data_converter

from longhaul.durable.payload_converter import guarded_data_converter
from longhaul.durable.payload_guard import DEFAULT_LIMIT, PayloadTooLargeError, json_size

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


class Note(BaseModel):
    text: str


def string_of_json_size(size: int) -> str:
    """A string whose JSON form is exactly `size` bytes: the text plus two quotes."""
    return "x" * (size - 2)


async def test_a_300_kb_payload_is_refused() -> None:
    with pytest.raises(PayloadTooLargeError) as caught:
        await guarded_data_converter.encode([Note(text="x" * 300_000)])
    assert caught.value.limit == DEFAULT_LIMIT == 256_000
    assert caught.value.size > 300_000


async def test_a_200_kb_payload_passes() -> None:
    (payload,) = await guarded_data_converter.encode([Note(text="x" * 200_000)])
    assert 200_000 < len(payload.data) < DEFAULT_LIMIT


async def test_the_limit_is_inclusive_like_the_activity_guard() -> None:
    at_limit = string_of_json_size(DEFAULT_LIMIT)
    assert json_size(at_limit) == DEFAULT_LIMIT
    (payload,) = await guarded_data_converter.encode([at_limit])
    assert len(payload.data) == DEFAULT_LIMIT

    with pytest.raises(PayloadTooLargeError) as caught:
        await guarded_data_converter.encode([at_limit + "x"])
    assert caught.value.size == DEFAULT_LIMIT + 1


async def test_the_message_says_what_to_do() -> None:
    with pytest.raises(PayloadTooLargeError, match="blob store"):
        await guarded_data_converter.encode(["x" * 300_000])


async def test_each_argument_is_judged_on_its_own() -> None:
    # An activity with two arguments sends two payloads. 400 KB in all is no reason to refuse
    # two payloads that are each under the limit: the cap is per payload (ADR-0005).
    payloads = await guarded_data_converter.encode(["a" * 200_000, "b" * 200_000])
    assert [len(p.data) for p in payloads] == [200_002, 200_002]


async def test_one_oversized_argument_refuses_the_whole_call() -> None:
    with pytest.raises(PayloadTooLargeError):
        await guarded_data_converter.encode(["small", "x" * 300_000, "small"])


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Note(text="x" * 300_000), id="model"),
        pytest.param({"transcript": ["x" * 100_000] * 3}, id="dict-of-a-list"),
        pytest.param(["x" * 100_000] * 3, id="list"),
        pytest.param("x" * 300_000, id="text"),
        pytest.param(b"x" * 300_000, id="bytes"),
    ],
)
async def test_every_kind_of_value_is_checked(value: Any) -> None:
    # bytes take the binary encoding and the rest take JSON. The guard looks at the payload, so
    # it does not depend on which converter made it.
    with pytest.raises(PayloadTooLargeError):
        await guarded_data_converter.encode([value])
    with pytest.raises(PayloadTooLargeError):
        guarded_data_converter.payload_converter.to_payload(value)
    with pytest.raises(PayloadTooLargeError):
        guarded_data_converter.payload_converter.to_payloads([value])


async def test_a_payload_that_comes_back_from_the_server_is_not_checked() -> None:
    # Only encode is guarded. A payload written before the guard existed, or by another client,
    # must still be readable, or a replay of an old history would fail.
    big = "x" * 300_000
    payload = pydantic_data_converter.payload_converter.to_payload(big)
    assert len(payload.data) > DEFAULT_LIMIT

    assert await guarded_data_converter.decode([payload]) == [big]


async def test_small_payloads_are_the_ones_the_pydantic_converter_makes() -> None:
    # The guard adds a check and changes nothing else, so histories written with the plain
    # converter stay readable and the other way round.
    value = [Note(text="a"), Note(text="b")]
    guarded = await guarded_data_converter.encode(value)
    plain = await pydantic_data_converter.encode(value)
    assert guarded == plain

    decoded = await guarded_data_converter.decode(plain, [Note, Note])
    assert decoded == value


async def test_a_payload_the_converter_returns_unchanged_is_checked_too() -> None:
    # RawValue passes through the SDK's converters as it is. The guard looks at what comes out,
    # so a ready-made payload cannot get around it.
    raw = RawValue(Payload(metadata={"encoding": b"binary/plain"}, data=b"x" * 300_000))
    with pytest.raises(PayloadTooLargeError):
        await guarded_data_converter.encode([raw])
