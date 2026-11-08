"""The pieces of the console login that need no server (LH-807).

The routes are checked against a database in tests/integration/test_api_auth.py. Here are the
rules that are plain functions: which addresses count as this machine, how the rate limit counts,
and what a deployment does when it has no token.
"""

from __future__ import annotations

import pytest

from longhaul.api.deps import is_loopback, open_context, token_matches
from longhaul.api.limits import ActionLimiter
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase8]


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "[::1]", "localhost"])
def test_these_hosts_are_this_machine(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "example.org", ""])
def test_these_hosts_are_reachable_from_the_network(host: str) -> None:
    assert not is_loopback(host)


def test_a_token_matches_only_itself() -> None:
    assert token_matches("abc", "abc")
    assert not token_matches("abd", "abc")
    assert not token_matches("ab", "abc")
    assert not token_matches("abcd", "abc")


@pytest.mark.parametrize("expected", [None, ""])
def test_no_token_set_matches_nothing(expected: str | None) -> None:
    # The empty string is what an unset variable in a compose file becomes.
    assert not token_matches("", expected)
    assert not token_matches("anything", expected)


def test_a_token_with_a_non_ascii_character_does_not_raise() -> None:
    assert not token_matches("café", "cafe")
    assert token_matches("café", "café")


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_the_limiter_allows_the_limit_and_then_says_how_long_to_wait() -> None:
    clock = Clock()
    limiter = ActionLimiter(limit=3, window_s=60, clock=clock)

    waits = []
    for _ in range(3):
        waits.append(limiter.take("a"))
        clock.now += 10
    refused = limiter.take("a")

    assert waits == [0.0, 0.0, 0.0]
    assert refused == pytest.approx(30.0)


def test_the_window_slides() -> None:
    clock = Clock()
    limiter = ActionLimiter(limit=2, window_s=60, clock=clock)
    limiter.take("a")  # at 0
    clock.now = 30
    limiter.take("a")  # at 30
    clock.now = 59
    assert limiter.take("a") > 0
    clock.now = 60  # the first one has aged out, the second has not
    assert limiter.take("a") == 0.0
    assert limiter.take("a") > 0


def test_a_refused_call_is_not_counted() -> None:
    clock = Clock()
    limiter = ActionLimiter(limit=1, window_s=60, clock=clock)
    limiter.take("a")
    for _ in range(10):
        clock.now += 5
        assert limiter.take("a") > 0
    clock.now = 60

    assert limiter.take("a") == 0.0


def test_each_token_has_its_own_count() -> None:
    limiter = ActionLimiter(limit=1, window_s=60, clock=Clock())

    assert limiter.take("a") == 0.0
    assert limiter.take("b") == 0.0
    assert limiter.take("a") > 0


def test_a_token_that_stopped_sending_is_forgotten() -> None:
    clock = Clock()
    limiter = ActionLimiter(limit=1, window_s=60, clock=clock)
    limiter.take("a")
    clock.now = 500
    limiter.take("b")

    assert limiter.keys() == {"b"}


@pytest.mark.parametrize("limit", [0, -1])
def test_the_limiter_needs_a_positive_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        ActionLimiter(limit=limit)


@pytest.mark.parametrize("token", [None, "", "   "])
async def test_a_deployment_without_a_token_does_not_start(
    monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    for name in ("LONGHAUL_ADMIN_TOKEN", "LONGHAUL_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    if token is not None:
        monkeypatch.setenv("LONGHAUL_ADMIN_TOKEN", token)

    with pytest.raises(ValueError, match="LONGHAUL_ADMIN_TOKEN"):
        async with open_context(EnvSettings(_env_file=None)):  # type: ignore[call-arg]
            pass
