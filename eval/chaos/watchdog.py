"""Restart the private Temporal when it has stopped answering (LH-904).

The Temporal dev server keeps its state in one SQLite connection. When a query on a busy machine
runs out of time, the driver can leave that connection interrupted for good: from then on every
call fails with "interrupted (9)", the workflow never moves, and a run waits for a server that
will not recover by itself. A restart clears it and keeps the workflows, because the state is in
a file on a volume.

The watchdog asks the server for something that needs its database. When that fails for
`wedged_after_s` in a row, it restarts the server. The limit is longer than a restart that the
suite asked for, so a kill is not mistaken for a wedge, and `reset` tells the watchdog about the
restarts that a kill makes.

A restart by the watchdog is counted in `heals` and shows in the summary, because a run that met
one saw a restart that the plan did not draw.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time
from collections.abc import Callable

from temporalio.client import Client

# A restart of the container takes under 20 s, and a server answers a few seconds after that.
WEDGED_AFTER_S = 60.0
INTERVAL_S = 10.0
PROBE_TIMEOUT_S = 15.0


def temporal_answers(address: str, timeout_s: float = PROBE_TIMEOUT_S) -> bool:
    """True when the server at `address` answers a call that reads its database.

    Counting workflows reads the visibility store, which sits in the same SQLite file as the
    rest, so a server whose connection is interrupted fails it. The connection is made again
    each time: a client keeps its calls on the event loop that made it.
    """

    async def ask() -> None:
        client = await Client.connect(address)
        await client.count_workflows()

    try:
        asyncio.run(asyncio.wait_for(ask(), timeout_s))
    except Exception:  # any failure to answer is the answer
        return False
    return True


class Watchdog:
    """Calls `heal` when `probe` has failed for `wedged_after_s` without a pause."""

    def __init__(
        self,
        probe: Callable[[], bool],
        heal: Callable[[], None],
        *,
        wedged_after_s: float = WEDGED_AFTER_S,
        interval_s: float = INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if wedged_after_s < 0:
            raise ValueError(f"wedged_after_s cannot be negative, not {wedged_after_s}")
        if interval_s <= 0:
            raise ValueError(f"the interval must be above 0 seconds, not {interval_s}")
        self._probe = probe
        self._heal = heal
        self._wedged_after_s = wedged_after_s
        self._interval_s = interval_s
        self._clock = clock
        self._failing_since: float | None = None
        self._heals = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def heals(self) -> int:
        """How many times a restart has been made, and went through."""
        return self._heals

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _answers(self) -> bool:
        try:
            return self._probe()
        except Exception:  # a probe that cannot run has not seen the server answer
            return False

    def check(self) -> bool:
        """Probe once. True when this probe led to a restart."""
        answered = self._answers()
        with self._lock:
            if answered:
                self._failing_since = None
                return False
            now = self._clock()
            if self._failing_since is None:
                self._failing_since = now
            if now - self._failing_since < self._wedged_after_s:
                return False
            # Whether the restart works or not, the server gets a whole limit to answer again.
            self._failing_since = None
        try:
            self._heal()
        except Exception as exc:
            print(f"watchdog: could not restart Temporal: {exc!r}", file=sys.stderr, flush=True)
            return False
        self._heals += 1
        return True

    def reset(self) -> None:
        """Forget the failures so far. A restart that the suite made on purpose calls this."""
        with self._lock:
            self._failing_since = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the watchdog was started already")
        self._thread = threading.Thread(target=self._loop, name="temporal-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=PROBE_TIMEOUT_S + self._interval_s)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval_s):
            with contextlib.suppress(Exception):  # a bug here must not end the watch
                self.check()
