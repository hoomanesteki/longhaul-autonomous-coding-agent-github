"""Redaction of the JSON the console API returns (LH-801).

The secrets are the ones telemetry.logging.redact knows. This module only walks structures, so
the tests check that it reaches every string and leaves everything else as it was.
"""

from __future__ import annotations

import pytest

from longhaul.api.redaction import redact_json

pytestmark = [pytest.mark.unit, pytest.mark.phase8]

KEY = "sk-" + "a1B2c3D4" * 4


def test_every_string_of_a_nested_value_is_redacted() -> None:
    value = {
        "text": f"key {KEY}",
        "list": [f"a {KEY}", {"deep": [f"b {KEY}"]}],
        f"name {KEY}": 1,
    }

    assert redact_json(value) == {
        "text": "key [redacted]",
        "list": ["a [redacted]", {"deep": ["b [redacted]"]}],
        "name [redacted]": 1,
    }


def test_numbers_booleans_and_none_are_left_alone() -> None:
    value = {"n": 3, "f": 1.5, "t": True, "none": None, "empty": [], "plain": "hello"}

    assert redact_json(value) == value


def test_the_input_is_not_changed() -> None:
    value = {"text": KEY, "items": [KEY]}

    redact_json(value)

    assert value == {"text": KEY, "items": [KEY]}


def test_a_tuple_comes_back_as_a_list() -> None:
    assert redact_json((KEY, 2)) == ["[redacted]", 2]
