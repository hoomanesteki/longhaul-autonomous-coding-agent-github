"""What the routes need, in one object, and how a deployment builds it.

`ApiContext` holds the four things the API talks to: the database, the blob store, the memory
store and Temporal. A route asks for it with `Ctx` and gets whatever the app was built with, so
a test hands in a scratch database and a test server, and `longhaul api` hands in the real ones.

Every route but `/healthz` checks the bearer token first (`authorize`). The token is
`LONGHAUL_ADMIN_TOKEN`, and a deployment that has none does not start: an API with no token would
be one that answers everyone. `/metrics` is open only when the server listens on this machine,
where a Prometheus beside it needs no secret and nobody else can reach the port.

Temporal is a function and not a client. The API serves the lists, the timelines and the memory
pages from the database alone, and it must come up, and stay up, while Temporal is down. The
client is made on the first request that needs it. A failed connection is not kept, so the next
request tries again.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from longhaul.api.limits import ActionLimiter
from longhaul.blobs.base import BlobStoreBase
from longhaul.blobs.s3 import S3BlobStore
from longhaul.db.session import database_url, make_engine
from longhaul.durable.client import connect
from longhaul.memory.embedder import LocalEmbedder
from longhaul.memory.store import PgMemoryStore
from longhaul.settings import EnvSettings

__all__ = [
    "ApiContext",
    "Ctx",
    "authorize",
    "authorize_metrics",
    "is_loopback",
    "open_context",
    "token_matches",
]

# How long a status query may wait for a worker before the page is shown without it. A query is
# answered by a worker, so with none running it would otherwise wait for the server's own limit.
STATUS_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class ApiContext:
    sessions: async_sessionmaker[AsyncSession]
    blobs: BlobStoreBase
    memory: PgMemoryStore
    temporal: Callable[[], Awaitable[Client]]
    status_timeout_s: float = STATUS_TIMEOUT_S
    # The bearer token of the API. None means none is set, and then every request is refused.
    admin_token: str | None = field(default=None, repr=False)
    # True when /metrics may be read without the token (the server listens on this machine).
    metrics_public: bool = False
    limiter: ActionLimiter = field(default_factory=ActionLimiter)


def context_of(request: Request) -> ApiContext:
    context: ApiContext = request.app.state.context
    return context


Ctx = Annotated[ApiContext, Depends(context_of)]


# auto_error is off so that a missing or malformed header gets the same 401 as a wrong token, and
# not FastAPI's own 403. The scheme still shows in the OpenAPI schema and in /docs.
bearer = HTTPBearer(auto_error=False, description="LONGHAUL_ADMIN_TOKEN")
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def token_matches(given: str, expected: str | None) -> bool:
    """Compare in constant time. A token that is not set matches nothing, not even the empty one.

    The strings go in as bytes: compare_digest refuses a str with a non-ASCII character, and a
    caller must not be able to turn that into a 500.
    """
    if not expected:
        return False
    return hmac.compare_digest(given.encode(), expected.encode())


def unauthorized() -> HTTPException:
    # One answer for a missing, a malformed and a wrong token, so it tells a stranger nothing.
    return HTTPException(
        status_code=401,
        detail="a valid bearer token is required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def checked_token(ctx: ApiContext, credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None or not token_matches(credentials.credentials, ctx.admin_token):
        raise unauthorized()
    return credentials.credentials


def authorize(request: Request, ctx: Ctx, credentials: Credentials) -> None:
    """The dependency of every `/api` route: a valid token, and for a POST, room in the limit.

    FastAPI runs a dependency before it reads the body or the query, so a caller with no token
    gets 401 and never learns what the route accepts.
    """
    token = checked_token(ctx, credentials)
    if request.method != "POST":
        return
    wait = ctx.limiter.take(token)
    if wait > 0:
        seconds = math.ceil(wait)
        raise HTTPException(
            status_code=429,
            detail=f"too many actions, try again in {seconds} seconds",
            headers={"Retry-After": str(seconds)},
        )


def authorize_metrics(ctx: Ctx, credentials: Credentials) -> None:
    if not ctx.metrics_public:
        checked_token(ctx, credentials)


def is_loopback(host: str) -> bool:
    """True when a server bound to `host` can only be reached from this machine."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:  # a name, or an empty setting
        return False


class LazyTemporal:
    """Connect to Temporal on first use, and keep the client once there is one."""

    def __init__(self, settings: EnvSettings) -> None:
        self._settings = settings
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    async def __call__(self) -> Client:
        async with self._lock:
            if self._client is None:
                self._client = await connect(
                    self._settings.temporal_address, namespace=self._settings.temporal_namespace
                )
            return self._client


@asynccontextmanager
async def open_context(settings: EnvSettings) -> AsyncIterator[ApiContext]:
    """The context of a deployment, from the LONGHAUL_* settings. The pool closes at the end.

    The token is required here and not in `ApiContext`, so that a test builds a context with
    whatever token it wants, and a deployment cannot start without one.

    The memory store gets the local embedder, which loads its model on first use. The API never
    embeds a text: it lists items and decides on them, and neither needs a vector.
    """
    token = settings.admin_token.get_secret_value() if settings.admin_token else ""
    if not token.strip():
        raise ValueError(
            "LONGHAUL_ADMIN_TOKEN is not set. The API refuses every request without a token. "
            "Make one with `openssl rand -hex 32`."
        )
    try:
        url = database_url()
        blobs: BlobStoreBase = S3BlobStore.from_settings(settings)
    except RuntimeError as exc:  # a setting is unset; say which one
        raise ValueError(str(exc)) from exc
    engine = make_engine(url)
    try:
        sessions = async_sessionmaker[AsyncSession](engine, expire_on_commit=False)
        yield ApiContext(
            sessions=sessions,
            blobs=blobs,
            memory=PgMemoryStore(sessions, LocalEmbedder()),
            temporal=LazyTemporal(settings),
            admin_token=token,
            metrics_public=is_loopback(settings.api_host),
        )
    finally:
        await engine.dispose()
