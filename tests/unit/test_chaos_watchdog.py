"""The watchdog of the private Temporal in the full chaos suite (LH-904).

The Temporal dev server keeps its state in one SQLite connection. A query that runs out of time
under load can leave that connection interrupted for good: every call then fails with "interrupted
(9)", and a run waits for a server that will never answer. The watchdog probes the server and
restarts it when the probe has failed for long enough. A restart keeps the workflows, which is what
the suite relies on anyway.
"""

import socket
import threading
import time
from collections.abc import Callable

import pytest

from eval.chaos import watchdog
from eval.chaos.watchdog import Watchdog

pytestmark = [pytest.mark.unit, pytest.mark.phase9]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(answers: list[bool], clock: Clock, *, after_s: float = 60.0) -> tuple[Watchdog, list[str]]:
    """A watchdog whose probe gives `answers` in turn, and the log of its restarts."""
    healed: list[str] = []
    replies = iter(answers)
    dog = Watchdog(
        lambda: next(replies),
        lambda: healed.append("restart"),
        wedged_after_s=after_s,
        interval_s=10.0,
        clock=clock,
    )
    return dog, healed


def test_a_server_that_answers_is_left_alone() -> None:
    clock = Clock()
    dog, healed = make([True] * 20, clock)

    for _ in range(20):
        assert dog.check() is False
        clock.now += 10

    assert healed == [] and dog.heals == 0


def test_a_server_that_keeps_failing_is_restarted_once_the_limit_has_passed() -> None:
    clock = Clock()
    dog, healed = make([False] * 10, clock, after_s=60.0)

    results = []
    for _ in range(8):
        results.append(dog.check())
        clock.now += 10

    # The first failure starts the clock at 1000. The check at 1060 is the first at 60 s.
    assert results == [False] * 6 + [True, False]
    assert healed == ["restart"] and dog.heals == 1


def test_a_failure_that_ends_before_the_limit_does_not_restart_anything() -> None:
    clock = Clock()
    dog, healed = make([False, False, False, True, False, False, False], clock, after_s=40.0)

    for _ in range(7):
        dog.check()
        clock.now += 10

    # Three failures in a row (20 s), an answer, and three more: neither run reaches 40 s.
    assert healed == []


def test_after_a_restart_the_server_gets_the_whole_limit_to_come_back() -> None:
    clock = Clock()
    dog, healed = make([False] * 30, clock, after_s=30.0)

    fired = []
    for _ in range(12):
        fired.append(dog.check())
        clock.now += 10

    assert fired == ([False] * 3 + [True]) * 3
    assert dog.heals == 3


def test_a_restart_that_the_suite_asked_for_resets_the_count() -> None:
    clock = Clock()
    dog, healed = make([False] * 10, clock, after_s=40.0)
    dog.check()
    clock.now += 30
    dog.check()  # 30 s of failures, 10 s short of a restart

    dog.reset()  # a kill restarted the server on purpose, which is down for a moment
    clock.now += 20
    dog.check()
    clock.now += 20
    fired = dog.check()  # 20 s since the reset

    assert fired is False and healed == []


def test_a_probe_that_raises_counts_as_a_failed_probe() -> None:
    clock = Clock()
    healed: list[str] = []

    def probe() -> bool:
        raise OSError("connection refused")

    dog = Watchdog(probe, lambda: healed.append("restart"), wedged_after_s=10.0, clock=clock)

    dog.check()
    clock.now += 10
    assert dog.check() is True
    assert healed == ["restart"]


def test_a_restart_that_fails_is_tried_again_after_another_limit() -> None:
    clock = Clock()
    calls: list[float] = []

    def heal() -> None:
        calls.append(clock.now)
        raise RuntimeError("docker is not answering")

    dog = Watchdog(lambda: False, heal, wedged_after_s=20.0, clock=clock)

    for _ in range(7):
        dog.check()
        clock.now += 10

    assert calls == [1020.0, 1050.0]
    assert dog.heals == 0, "a restart that failed is not counted"


def test_the_limit_and_interval_must_make_sense() -> None:
    with pytest.raises(ValueError, match="interval"):
        Watchdog(lambda: True, lambda: None, interval_s=0)
    with pytest.raises(ValueError, match="wedged_after_s"):
        Watchdog(lambda: True, lambda: None, wedged_after_s=-1)


# ---------- the thread ----------
def wait_until(condition: Callable[[], bool], seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_the_thread_restarts_a_server_that_never_answers_and_stops_when_told() -> None:
    restarted = threading.Event()
    dog = Watchdog(lambda: False, restarted.set, wedged_after_s=0.0, interval_s=0.01)

    dog.start()
    try:
        assert restarted.wait(5.0)
    finally:
        dog.stop()

    assert dog.heals >= 1
    assert not dog.running


def test_a_watchdog_that_was_never_started_can_be_stopped() -> None:
    Watchdog(lambda: True, lambda: None).stop()


def test_a_watchdog_cannot_be_started_twice() -> None:
    dog = Watchdog(lambda: True, lambda: None, interval_s=0.01)
    dog.start()
    try:
        with pytest.raises(RuntimeError, match="started"):
            dog.start()
    finally:
        dog.stop()


# ---------- the probe ----------
def test_the_probe_says_no_for_an_address_where_nothing_listens() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
    began = time.monotonic()

    assert watchdog.temporal_answers(f"127.0.0.1:{port}", timeout_s=3.0) is False
    assert time.monotonic() - began < 10
