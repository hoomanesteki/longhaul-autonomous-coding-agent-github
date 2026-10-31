"""Alembic environment, async-capable.

The database comes from, in order:

1. config.attributes["connection"], an open sync connection that the caller owns. Code that
   already runs inside an event loop must use this, because a second loop cannot be started
   there. Call the alembic command from AsyncConnection.run_sync.
2. config.attributes["url"], then the sqlalchemy.url option set on the Config in code.
3. LONGHAUL_DATABASE_URL through the app settings, so a value in .env works too.

There is no built-in default URL. A silent fallback could migrate, or downgrade, a database
the caller did not mean.
"""

import asyncio
from typing import Any

from alembic import context
from sqlalchemy.engine import Connection

from longhaul.db.models import Base
from longhaul.db.session import database_url, make_engine

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    return database_url(config.attributes.get("url"), config.get_main_option("sqlalchemy.url"))


def _configure(**kwargs: Any) -> None:
    # compare_server_default makes autogenerate and `alembic check` notice a changed default.
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_url(), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    # NullPool: the engine lives for one asyncio.run, so nothing may wait in a pool.
    engine = make_engine(_database_url(), pooled=False)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_sync(connection)
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(run_migrations_online())
    else:
        raise RuntimeError(
            "alembic was started inside a running event loop, which cannot start another. "
            "Pass an open connection in config.attributes['connection'] and call the "
            "command from AsyncConnection.run_sync."
        )


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_online()
