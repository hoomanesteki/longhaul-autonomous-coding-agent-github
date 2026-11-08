"""Redaction of everything the console API returns that a model or a tool wrote.

Turns, tool outputs, event payloads and memory notes can hold text that came from a repo or a
shell, and a secret can be in any of it. The patterns are the ones of the logger
(telemetry.logging.redact), so one function decides what counts as a secret. The API applies it
when it reads a stored value, never when the value is stored: the blob store keeps what the model
saw, and the console shows less.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from longhaul.telemetry.logging import redact

__all__ = ["redact", "redact_json"]


def redact_json(value: Any) -> Any:
    """A copy of a JSON value with every secret in its strings and keys replaced.

    Anything that is not a string, a mapping or a list (a number, a boolean, None) comes back as
    it is. A tuple becomes a list, which is what JSON has.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {redact(str(key)): redact_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [redact_json(item) for item in value]
    return value
