import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic import context as alembic_context
from alembic.config import Config
from sqlalchemy.pool import NullPool

from longhaul.db.session import (
    database_url,
    dispose_engine,
    get_engine,
    get_session,
    make_engine,
    to_async_url,
)
from longhaul.settings import get_settings

pytestmark = [pytest.mark.unit, pytest.mark.phase0]

ROOT = Path(__file__).resolve().parents[2]


def test_postgresql_url_gets_asyncpg_driver() -> None:
    assert (
        to_async_url("postgresql://u:p@localhost:5432/db")
        == "postgresql+asyncpg://u:p@localhost:5432/db"
    )


def test_postgres_alias_and_existing_driver() -> None:
    assert to_async_url("postgres://u@h/db") == "postgresql+asyncpg://u@h/db"
    assert to_async_url("postgresql+asyncpg://u@h/db") == "postgresql+asyncpg://u@h/db"


def test_make_engine_uses_asyncpg() -> None:
    engine = make_engine("postgresql://u:p@localhost:5432/db")
    assert engine.dialect.driver == "asyncpg"


def test_make_engine_can_skip_the_pool() -> None:
    # Pooled asyncpg connections stay bound to the loop that opened them, so one-shot
    # callers such as migrations ask for an engine that closes every connection on release.
    assert not isinstance(make_engine("postgresql://u:p@h/db").pool, NullPool)
    assert isinstance(make_engine("postgresql://u:p@h/db", pooled=False).pool, NullPool)


@pytest.fixture
def fresh_caches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Run from an empty directory so a developer's own .env cannot supply a database URL.
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_get_engine_requires_database_url(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    monkeypatch.delenv("LONGHAUL_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="LONGHAUL_DATABASE_URL"):
        get_engine()


def test_blank_database_url_counts_as_missing(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", "")
    with pytest.raises(RuntimeError, match="LONGHAUL_DATABASE_URL is not set"):
        get_engine()


async def test_get_session_builds_session_without_connecting(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", "postgresql://u:p@localhost:1/db")
    assert get_engine() is get_engine()
    async with get_session() as session:
        assert session.bind is get_engine()


async def test_dispose_engine_drops_the_cached_engine(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", "postgresql://u:p@localhost:1/db")
    first = get_engine()
    pool = first.pool

    await dispose_engine()

    assert get_engine.cache_info().currsize == 0
    assert first.pool is not pool  # SQLAlchemy swaps in a new pool after closing the old one
    assert get_engine() is not first


async def test_dispose_engine_without_an_engine_does_nothing(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    monkeypatch.delenv("LONGHAUL_DATABASE_URL", raising=False)

    await dispose_engine()  # must not build an engine, which would need the missing URL

    assert get_engine.cache_info().currsize == 0


def test_database_url_prefers_explicit_urls_then_settings(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", "postgresql://u:p@env/db")

    assert database_url("postgresql://u:p@first/db", "postgresql://u:p@second/db") == (
        "postgresql://u:p@first/db"
    )
    assert database_url(None, "", "postgresql://u:p@second/db") == "postgresql://u:p@second/db"
    assert database_url(None, "") == "postgresql://u:p@env/db"
    assert database_url() == "postgresql://u:p@env/db"


def test_database_url_without_a_source_has_no_default(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    # A built-in fallback could point a downgrade at a database the caller did not mean.
    monkeypatch.delenv("LONGHAUL_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="LONGHAUL_DATABASE_URL is not set"):
        database_url(None, "")


# ---------- migrations/env.py ----------
# These run alembic's offline mode, which renders SQL and never connects, so they need
# no database. A spy on alembic's configure() shows which URL env.py resolved.


def _alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"), output_buffer=io.StringIO())
    cfg.set_main_option("script_location", str(ROOT / "src/longhaul/db/migrations"))
    return cfg


@pytest.fixture
def configure_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    real = alembic_context.configure

    def spy(**kwargs: Any) -> None:
        calls.append(kwargs)
        real(**kwargs)

    monkeypatch.setattr(alembic_context, "configure", spy)
    return calls


ATTR = "postgresql+asyncpg://u:p@attribute/db"
OPTION = "postgresql+asyncpg://u:p@option/db"
ENV = "postgresql+asyncpg://u:p@settings/db"


@pytest.mark.parametrize(
    ("attribute", "option", "expected"),
    [
        pytest.param(ATTR, OPTION, ATTR, id="attribute beats option and settings"),
        pytest.param(None, OPTION, OPTION, id="option beats settings"),
        pytest.param(None, None, ENV, id="settings are the last resort"),
    ],
)
def test_migration_env_resolves_url_in_order(
    monkeypatch: pytest.MonkeyPatch,
    fresh_caches: None,
    configure_calls: list[dict[str, Any]],
    attribute: str | None,
    option: str | None,
    expected: str,
) -> None:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", ENV)
    cfg = _alembic_config()
    if attribute is not None:
        cfg.attributes["url"] = attribute
    if option is not None:
        cfg.set_main_option("sqlalchemy.url", option)

    command.upgrade(cfg, "head", sql=True)

    assert str(configure_calls[0]["url"]) == expected


def test_migration_env_compares_types_and_server_defaults(
    monkeypatch: pytest.MonkeyPatch,
    fresh_caches: None,
    configure_calls: list[dict[str, Any]],
) -> None:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", ENV)

    command.upgrade(_alembic_config(), "head", sql=True)

    assert configure_calls[0]["compare_type"] is True
    assert configure_calls[0]["compare_server_default"] is True


def test_migration_env_has_no_default_database(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    monkeypatch.delenv("LONGHAUL_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="LONGHAUL_DATABASE_URL is not set"):
        command.upgrade(_alembic_config(), "head", sql=True)


def test_migration_env_uses_an_unpooled_engine(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    class Stop(Exception):
        pass

    seen: dict[str, Any] = {}

    def fake_make_engine(url: str, *, pooled: bool = True) -> None:
        seen.update(url=url, pooled=pooled)
        raise Stop

    monkeypatch.setattr("longhaul.db.session.make_engine", fake_make_engine)
    cfg = _alembic_config()
    cfg.set_main_option("sqlalchemy.url", OPTION)

    with pytest.raises(Stop):
        command.current(cfg)

    assert seen == {"url": OPTION, "pooled": False}


async def test_migration_env_in_a_running_loop_asks_for_a_connection(
    fresh_caches: None,
) -> None:
    # asyncio.run cannot nest, so env.py explains the supported route instead of failing
    # with asyncio's message. No connection is attempted before the check.
    cfg = _alembic_config()
    cfg.set_main_option("sqlalchemy.url", OPTION)

    with pytest.raises(RuntimeError, match=r"config\.attributes\['connection'\]"):
        command.current(cfg)
