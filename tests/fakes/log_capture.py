"""Read back the JSON lines that setup_logging writes (LH-706).

setup_logging replaces the handlers of the root logger and sets structlog's process-wide
configuration. A test that calls it has to put both back, or every later test in the process
logs into a stream that nobody reads. `captured_logs` does that, and hands the test the lines
as dictionaries.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import structlog

from longhaul.telemetry.logging import setup_logging


class Captured:
    """The stream behind setup_logging, read as one dictionary per line."""

    def __init__(self, stream: io.StringIO) -> None:
        self.stream = stream

    def text(self) -> str:
        return self.stream.getvalue()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text().splitlines() if line.strip()]

    def named(self, event: str) -> dict[str, Any]:
        """The one line whose event is exactly this text."""
        (found,) = [line for line in self.lines() if line.get("event") == event]
        return found


@contextmanager
def captured_logs(level: int | str = logging.INFO) -> Iterator[Captured]:
    root = logging.getLogger()
    handlers, root_level = root.handlers[:], root.level
    stream = io.StringIO()
    setup_logging(level, stream=stream)
    try:
        yield Captured(stream)
    finally:
        root.handlers[:] = handlers
        root.setLevel(root_level)
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
