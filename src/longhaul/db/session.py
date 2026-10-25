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

from longhaul.settings import get_settings

_SYNC_PREFIXES = ("postgresql://", "postgres://")


def to_async_url(url: str) -> str:
    """Point a plain postgres URL at the asyncpg driver."""
    for prefix in _SYNC_PREFIXES:
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(to_async_url(url), pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    secret = get_settings().database_url
    if secret is None:
        raise RuntimeError("LONGHAUL_DATABASE_URL is not set")
    return make_engine(secret.get_secret_value())


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    async with factory() as session:
        yield session
