"""Alembic environment, async-capable."""

import asyncio
import os
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection

from longhaul.db.models import Base
from longhaul.db.session import make_engine

target_metadata = Base.metadata
DEFAULT_URL = "postgresql://longhaul:longhaul@localhost:5432/longhaul"


def _database_url() -> str:
    return os.environ.get("LONGHAUL_DATABASE_URL", DEFAULT_URL)


def _configure(**kwargs: Any) -> None:
    context.configure(target_metadata=target_metadata, compare_type=True, **kwargs)


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = make_engine(_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
