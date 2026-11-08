"""What the routes need, in one object, and how a deployment builds it.

`ApiContext` holds the four things the API talks to: the database, the blob store, the memory
store and Temporal. A route asks for it with `Ctx` and gets whatever the app was built with, so
a test hands in a scratch database and a test server, and `longhaul api` hands in the real ones.

Temporal is a function and not a client. The API serves the lists, the timelines and the memory
pages from the database alone, and it must come up, and stay up, while Temporal is down. The
client is made on the first request that needs it. A failed connection is not kept, so the next
request tries again.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from longhaul.blobs.base import BlobStoreBase
from longhaul.blobs.s3 import S3BlobStore
from longhaul.db.session import database_url, make_engine
from longhaul.durable.client import connect
from longhaul.memory.embedder import LocalEmbedder
from longhaul.memory.store import PgMemoryStore
from longhaul.settings import EnvSettings

__all__ = ["ApiContext", "Ctx", "open_context"]

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


def context_of(request: Request) -> ApiContext:
    context: ApiContext = request.app.state.context
    return context


Ctx = Annotated[ApiContext, Depends(context_of)]


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

    The memory store gets the local embedder, which loads its model on first use. The API never
    embeds a text: it lists items and decides on them, and neither needs a vector.
    """
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
        )
    finally:
        await engine.dispose()
