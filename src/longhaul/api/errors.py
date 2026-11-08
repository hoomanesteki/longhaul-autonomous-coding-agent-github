"""How failures of the stores become answers.

The routes raise these and let FastAPI turn them into a status and a `detail` line. The mapping
is kept in one place so that every route says the same thing about the same failure:

* 400 for a cursor that is not ours, 416 for a range of lines beyond the end of the text, and 422
  (FastAPI's own) for a bad parameter or body.
* 404 for a task, call, turn or item that does not exist, and 409 for a signal sent to a task that
  has ended: the person can tell "no such task" from "too late".
* 401 for a missing or wrong bearer token, and 429 for a token that sends more than 30 actions a
  minute (deps.authorize). They come before any other check, so they hide nothing.
* 502 when the blob store fails or holds something unreadable, and 503 when Temporal or the
  database cannot be reached. The page shows the line and the rest of the console goes on.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from pydantic import ValidationError
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from longhaul.blobs.base import BlobError

log = logging.getLogger(__name__)

__all__ = [
    "PROTECTED",
    "TEMPORAL_ERRORS",
    "STORE_ERRORS",
    "not_found",
    "temporal_failure",
    "with_client",
]

# What every route behind the token can answer besides its own replies, for the OpenAPI schema.
PROTECTED: dict[int | str, dict[str, str]] = {
    401: {"description": "The bearer token is missing or wrong."},
}

# What a call to Temporal raises when the server is down or does not know the workflow.
TEMPORAL_ERRORS = (ConnectionError, OSError, RPCError, TimeoutError)
# What reading a stored blob raises: a store that is down or damaged, or JSON that does not fit.
STORE_ERRORS = (BlobError, BotoCoreError, ClientError, ValidationError, ValueError)


def not_found(what: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no such {what}")


def temporal_failure(exc: Exception) -> HTTPException:
    """The answer for an error from Temporal while sending a signal."""
    if isinstance(exc, RPCError) and exc.status is RPCStatusCode.NOT_FOUND:
        # The server says the same status for a workflow it never had and one that has ended.
        if "completed" in exc.message.lower():
            return HTTPException(status_code=409, detail="the task has ended")
        return HTTPException(status_code=404, detail="no such running task")
    log.warning("temporal request failed: %s: %s", type(exc).__name__, exc)
    return HTTPException(status_code=503, detail="Temporal is not reachable")


async def with_client[T](
    connect: Callable[[], Awaitable[Client]], job: Callable[[Client], Awaitable[T]]
) -> T:
    """Run `job` with the Temporal client, and turn a failure into an HTTPException."""
    try:
        return await job(await connect())
    except TEMPORAL_ERRORS as exc:
        raise temporal_failure(exc) from exc


def unreadable(what: str, exc: Exception) -> HTTPException:
    """The answer for a stored blob that could not be read or parsed."""
    log.warning("cannot read %s: %s: %s", what, type(exc).__name__, exc)
    return HTTPException(status_code=502, detail=f"{what} could not be read from the store")
