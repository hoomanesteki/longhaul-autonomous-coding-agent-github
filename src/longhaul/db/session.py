"""Async engine and session helpers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from longhaul.settings import get_settings

_SYNC_PREFIXES = ("postgresql://", "postgres://")


def to_async_url(url: str) -> str:
    """Point a plain postgres URL at the asyncpg driver."""
    for prefix in _SYNC_PREFIXES:
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


def database_url(*preferred: str | None) -> str:
    """Return the first non-empty URL in preferred, else LONGHAUL_DATABASE_URL from settings.

    There is no built-in default. A silent fallback to localhost could send a migration or a
    downgrade to a database the caller did not mean.
    """
    for url in preferred:
        if url:
            return url
    secret = get_settings().database_url
    if secret is None:
        raise RuntimeError("LONGHAUL_DATABASE_URL is not set")
    return secret.get_secret_value()


def make_engine(url: str, *, pooled: bool = True) -> AsyncEngine:
    """Build an async engine.

    A pooled asyncpg connection stays bound to the event loop that opened it. Callers that
    run one short loop, such as migrations, pass pooled=False so each connection is closed
    when it is released instead of waiting in a pool for a loop that is gone.
    """
    if pooled:
        return create_async_engine(to_async_url(url), pool_pre_ping=True)
    return create_async_engine(to_async_url(url), poolclass=NullPool)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    return make_engine(database_url())


async def dispose_engine() -> None:
    """Close the cached engine's pooled connections and forget the engine.

    Call it on the loop that used the engine, before that loop closes: at the end of an
    asyncio.run() block, or in the teardown of a test that runs on its own loop. The next
    get_engine() call then builds a fresh engine for the new loop.
    """
    if get_engine.cache_info().currsize == 0:
        return
    engine = get_engine()
    get_engine.cache_clear()
    await engine.dispose()


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        yield session
