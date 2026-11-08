"""A limit on how often one token may send actions (LH-807).

An action is a POST: it changes a task or a memory item. The console sends a few a minute, so 30
a minute stops a script that loops or a stolen token that is trying everything, and never stops a
person.

The count is a sliding window: the times of the last accepted actions of each token. A fixed
minute that resets on the clock would let 30 through at 12:00:59 and 30 more at 12:01:00. The
window lives in this process. With more than one API process each would count on its own, so the
real limit would be 30 times the number of processes. The console API is a single process
(ADR-0011), and a shared counter belongs to the day it is not.

Only a token that passed the check reaches the limiter (deps.authorize), so a caller who guesses
tokens cannot fill the table with keys.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

__all__ = ["ACTIONS_PER_MINUTE", "ActionLimiter"]

ACTIONS_PER_MINUTE = 30
WINDOW_S = 60.0


class ActionLimiter:
    def __init__(
        self,
        limit: int = ACTIONS_PER_MINUTE,
        window_s: float = WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1:
            raise ValueError(f"limit must be at least 1, not {limit}")
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._taken: dict[str, deque[float]] = {}

    def take(self, key: str) -> float:
        """Count one action of `key`. Returns 0.0 when it may go ahead, and otherwise the seconds
        until it may. A refused action is not counted, so waiting that long always works."""
        now = self._clock()
        self._forget_idle(now)
        times = self._taken.setdefault(key, deque())
        if len(times) >= self._limit:
            return times[0] + self._window_s - now
        times.append(now)
        return 0.0

    def keys(self) -> set[str]:
        return set(self._taken)

    def _forget_idle(self, now: float) -> None:
        """Drop the actions that left the window, and the keys with none left."""
        edge = now - self._window_s
        for key in list(self._taken):
            times = self._taken[key]
            while times and times[0] <= edge:
                times.popleft()
            if not times:
                del self._taken[key]
