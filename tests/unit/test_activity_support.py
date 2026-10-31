"""The helpers that the LLM and tool activities share: heartbeats, the payload guard, the
mapping of provider errors to Temporal errors, and safe blob names.

Activities run here inside temporalio's ActivityEnvironment, which needs no server.
"""

import asyncio
import dataclasses
from datetime import timedelta
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from longhaul.blobs.base import blob_key
from longhaul.contracts import ProviderError, ProviderErrorKind
from longhaul.durable import acts
from longhaul.durable.activities import support
from longhaul.durable.payload_guard import PayloadTooLargeError

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


def environment(heartbeat_timeout_s: float | None = None) -> tuple[ActivityEnvironment, list[Any]]:
    env = ActivityEnvironment()
    timeout = None if heartbeat_timeout_s is None else timedelta(seconds=heartbeat_timeout_s)
    env.info = dataclasses.replace(env.info, heartbeat_timeout=timeout)
    beats: list[Any] = []
    env.on_heartbeat = lambda *details: beats.append(details)
    return env, beats


# ---------- heartbeats ----------
async def test_the_default_interval_is_ten_seconds() -> None:
    assert support.HEARTBEAT_INTERVAL_S == 10.0


async def test_heartbeating_sends_one_at_once_and_none_after_the_body_ends() -> None:
    env, beats = environment()  # no heartbeat timeout: the interval stays at ten seconds

    async def body() -> None:
        async with support.heartbeating():
            await asyncio.sleep(0.05)

    await env.run(body)
    assert len(beats) == 1
    await asyncio.sleep(0.05)
    assert len(beats) == 1


async def test_heartbeating_keeps_inside_the_heartbeat_timeout() -> None:
    # The server fails an activity that is silent for a whole heartbeat timeout, so the interval
    # is a third of it. A test run uses 2 s timeouts (acts.Tuning), and ten seconds would be late.
    env, beats = environment(heartbeat_timeout_s=0.3)

    async def body() -> None:
        async with support.heartbeating():
            await asyncio.sleep(0.55)

    await env.run(body)
    assert len(beats) >= 4


async def test_heartbeating_never_waits_longer_than_the_interval_it_was_given() -> None:
    env, beats = environment(heartbeat_timeout_s=3600)

    async def body() -> None:
        async with support.heartbeating(0.1):
            await asyncio.sleep(0.35)

    await env.run(body)
    assert len(beats) >= 3


async def test_heartbeating_stops_when_the_body_fails() -> None:
    env, beats = environment(heartbeat_timeout_s=0.3)

    async def body() -> None:
        async with support.heartbeating():
            raise RuntimeError("the tool broke")

    with pytest.raises(RuntimeError, match="the tool broke"):
        await env.run(body)
    seen = len(beats)
    await asyncio.sleep(0.3)
    assert len(beats) == seen


async def test_heartbeating_does_not_hold_back_a_cancel() -> None:
    env, _ = environment(heartbeat_timeout_s=0.3)
    started = asyncio.Event()

    async def body() -> None:
        async with support.heartbeating():
            started.set()
            await asyncio.sleep(30)

    task = asyncio.ensure_future(env.run(body))
    await started.wait()
    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


# ---------- the payload guard (I2) ----------
def test_small_returns_what_it_was_given() -> None:
    payload = {"a": 1}
    assert support.small(payload) is payload


def test_small_turns_an_oversized_payload_into_a_failure_that_is_not_retried() -> None:
    with pytest.raises(ApplicationError) as caught:
        support.small({"transcript": "x" * 300_000})
    assert caught.value.non_retryable
    assert caught.value.type == PayloadTooLargeError.__name__
    assert "blob store" in str(caught.value)


def test_the_policies_do_not_retry_the_type_small_raises() -> None:
    # The error type is what a RetryPolicy matches, so the two must name the same thing.
    for act in acts.ALL:
        assert support.PAYLOAD_ERROR_TYPE in (act.retry.non_retryable_error_types or [])


# ---------- provider errors ----------
RETRYABLE = {
    ProviderErrorKind.RATE_LIMIT,
    ProviderErrorKind.OVERLOADED,
    ProviderErrorKind.SERVER,
    ProviderErrorKind.TIMEOUT,
}


@pytest.mark.parametrize("kind", list(ProviderErrorKind))
def test_a_provider_error_keeps_its_kind_as_the_error_type(kind: ProviderErrorKind) -> None:
    failure = support.provider_failure(ProviderError(kind, "the provider said no"))
    assert failure.type == kind.value
    assert failure.message == "the provider said no"
    assert failure.non_retryable == (kind not in RETRYABLE)


def test_the_kinds_the_policy_never_retries_are_the_non_retryable_ones() -> None:
    # acts.LLM_TURN names three kinds, and the helper has to agree with it.
    never = set(acts.LLM_TURN.retry.non_retryable_error_types or [])
    for kind in ProviderErrorKind:
        failure = support.provider_failure(ProviderError(kind, "m"))
        if failure.non_retryable:
            assert kind.value in never, kind


def test_a_retry_after_hint_sets_the_delay_of_the_next_attempt() -> None:
    failure = support.provider_failure(
        ProviderError(ProviderErrorKind.RATE_LIMIT, "slow down", retry_after_s=7.5)
    )
    assert failure.next_retry_delay == timedelta(seconds=7.5)


def test_a_retry_after_hint_is_capped_at_the_longest_backoff_of_the_policy() -> None:
    failure = support.provider_failure(
        ProviderError(ProviderErrorKind.RATE_LIMIT, "slow down", retry_after_s=3600)
    )
    assert failure.next_retry_delay == acts.LLM_TURN.retry.maximum_interval


def test_no_hint_leaves_the_delay_to_the_policy() -> None:
    failure = support.provider_failure(ProviderError(ProviderErrorKind.OVERLOADED, "busy"))
    assert failure.next_retry_delay is None


def test_a_hint_on_an_error_that_is_not_retried_is_dropped() -> None:
    failure = support.provider_failure(
        ProviderError(ProviderErrorKind.AUTH, "bad key", retry_after_s=5)
    )
    assert failure.next_retry_delay is None


# ---------- errors that retrying cannot fix ----------
def test_a_failure_that_is_not_retried_names_the_exception_type() -> None:
    failure = support.not_retried(ValueError("no system prompt is written for the planner role"))
    assert failure.non_retryable
    assert failure.type == "ValueError"
    assert "planner" in str(failure)


# ---------- blob names ----------
@pytest.mark.parametrize("call_id", ["toolu_01ABC", "call_abc123", "toolu_a1b2c3d4e5f6a7b8_0"])
def test_an_id_made_of_safe_characters_is_used_as_it_is(call_id: str) -> None:
    assert support.call_slug(call_id) == call_id


@pytest.mark.parametrize(
    "call_id",
    [
        "functions.get_weather:0",
        "../../etc/passwd",
        "a..b",
        "",
        "_leading",
        "ünïcode",
        "x" * 300,
        "has space",
    ],
)
def test_any_other_id_becomes_a_valid_blob_name_part(call_id: str) -> None:
    slug = support.call_slug(call_id)
    # blob_key refuses a part that could climb out of its folder or break a shell command.
    key = blob_key("lh-x", "turns", 3, f"tool-{slug}-output.txt")
    assert key.startswith("tasks/lh-x/turns/0003/tool-")
    assert len(slug) <= 80


def test_different_odd_ids_do_not_share_a_name() -> None:
    first = support.call_slug("functions.get:0")
    second = support.call_slug("functions.get:1")
    assert first != second
