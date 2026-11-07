"""What the LLM and tool activities share: heartbeats, the payload guard and error mapping.

Activities run on a worker and may do I/O. Nothing here is imported by workflow code.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import AsyncIterator
from datetime import timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

# call_slug lives with the other blob naming rules now (blobs/base.py), because output handles
# need it outside the activities. It is re-exported so that code that asks the activities'
# helper module for it keeps working.
from longhaul.blobs.base import BlobError
from longhaul.blobs.base import call_slug as call_slug
from longhaul.context.tokens import TokenMeter
from longhaul.contracts import BlobRef, LLMProvider, ProviderError
from longhaul.durable import acts
from longhaul.durable.payload_guard import PayloadTooLargeError, assert_small
from longhaul.durable.scripts import TaskProviders
from longhaul.github.client import GitHubError, GitHubRetryable

# How often an activity tells the server that it is alive (plan section 3.7 lists 15 to 60
# second heartbeat timeouts, and the SDK sends at most one heartbeat per 0.8 timeouts).
HEARTBEAT_INTERVAL_S = 10.0

# The error type that retry policies match on (acts._OVERSIZED names the same class).
PAYLOAD_ERROR_TYPE = PayloadTooLargeError.__name__

NO_PROVIDER = (
    "this worker has no model provider of its own: LONGHAUL_PROVIDER is scripted and no script "
    "is set. Start the task with a script (longhaul run --script FILE) or give the worker "
    "LONGHAUL_SCRIPT"
)
# How many tasks a worker keeps a token meter for. A worker plays a handful of tasks at once.
METER_CAPACITY = 64


# ---------- heartbeats ----------
@contextlib.asynccontextmanager
async def heartbeating(interval_s: float = HEARTBEAT_INTERVAL_S) -> AsyncIterator[None]:
    """Heartbeat from a background task while the body runs.

    The body awaits a model call or a command, and a heartbeat sent between two awaits of the
    body itself would stop for as long as one of them takes. A task of its own does not. It also
    cannot work if the body blocks the event loop, so activity code must await its blocking
    calls through asyncio.to_thread (ADR-0004).

    The server fails an activity that stays silent for a whole heartbeat timeout, and a test or
    a chaos run shortens that timeout to a few seconds (acts.Tuning). So the interval is a
    third of the timeout when there is one and it is shorter than `interval_s`.
    """
    timeout = activity.info().heartbeat_timeout
    wait = interval_s if timeout is None else min(interval_s, timeout.total_seconds() / 3)

    async def beat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(wait)

    beater = asyncio.create_task(beat())
    try:
        yield
    finally:
        beater.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beater


# ---------- the payload guard (I2) ----------
def small[T](payload: T) -> T:
    """Return the payload if it serializes under 256 KB, else fail the activity for good.

    Retrying cannot make a payload smaller (ADR-0005), so the error is not retryable.
    """
    try:
        assert_small(payload)
    except PayloadTooLargeError as exc:
        raise ApplicationError(str(exc), type=PAYLOAD_ERROR_TYPE, non_retryable=True) from exc
    return payload


# ---------- errors ----------
def provider_failure(exc: ProviderError) -> ApplicationError:
    """A provider error as the Temporal error that carries its kind and its retry advice.

    The kind is the error type, which the retry policy of llm_turn matches on. A kind that no
    retry can fix is marked non-retryable here as well, as the policy's second guard. When the
    provider said how long to wait, the next attempt waits that long, but never longer than the
    policy's own longest backoff.
    """
    delay = None
    if exc.retry_after_s is not None and exc.retryable:
        delay = timedelta(seconds=exc.retry_after_s)
        longest = acts.LLM_TURN.retry.maximum_interval
        if longest is not None:
            delay = min(delay, longest)
    return ApplicationError(
        exc.message, type=exc.kind.value, non_retryable=not exc.retryable, next_retry_delay=delay
    )


def github_failure(exc: GitHubError) -> ApplicationError:
    """A GitHub error as the Temporal error that carries its retry advice (failure mode F10).

    GitHubRetryable stays retryable and hands GitHub's own wait to the next attempt. Every
    other GitHub error is one that asking again cannot fix.
    """
    if isinstance(exc, GitHubRetryable):
        wait = exc.retry_after_s
        delay = None if wait is None else timedelta(seconds=wait)
        return ApplicationError(str(exc), type="GitHubRetryable", next_retry_delay=delay)
    return ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True)


def not_retried(exc: Exception) -> ApplicationError:
    """A failure that the same input would repeat: a transcript the API rejects, a bad role."""
    return ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True)


# ---------- who answers a model call ----------
async def provider_for_task(
    scripts: TaskProviders, default: LLMProvider | None, task_id: str, script_ref: BlobRef | None
) -> LLMProvider:
    """The scripted provider of the task if it has a script, else the provider of the worker.

    Every activity that calls a model asks this, so a scripted task is answered by its script
    whichever activity makes the call. A script that does not load, or that the store no longer
    has, stays that way, and so does a worker with no provider and a task with no script. All
    of them fail for good. A store that cannot be reached raises something else, and Temporal
    retries.
    """
    if script_ref is not None:
        try:
            return await scripts.provider_for(task_id, script_ref)
        except (ValueError, BlobError) as exc:
            raise not_retried(exc) from exc
    if default is None:
        raise not_retried(ValueError(NO_PROVIDER))
    return default


# ---------- token meters ----------
class Meters:
    """One TokenMeter for each task the worker is running.

    A meter learns how many characters a token takes from the replies of a task, so the next
    request can be projected from its characters alone (context/tokens.py). The activities of a
    task run one after the other on whatever worker is free, and a worker that has not seen the
    task yet starts from the default rule, which is right to about a fifth. The meters of the
    oldest tasks are dropped when more than `capacity` are held, and a dropped task starts over.
    """

    def __init__(self, capacity: int = METER_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity has to be at least 1, got {capacity}")
        self._capacity = capacity
        self._meters: OrderedDict[str, TokenMeter] = OrderedDict()

    def for_task(self, task_id: str) -> TokenMeter:
        meter = self._meters.get(task_id)
        if meter is None:
            meter = self._meters[task_id] = TokenMeter()
        self._meters.move_to_end(task_id)
        while len(self._meters) > self._capacity:
            self._meters.popitem(last=False)
        return meter
