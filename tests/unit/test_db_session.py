from collections.abc import Iterator

import pytest

from longhaul.db.session import get_engine, get_session, make_engine, to_async_url
from longhaul.settings import get_settings

pytestmark = [pytest.mark.unit, pytest.mark.phase0]


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


@pytest.fixture
def fresh_caches() -> Iterator[None]:
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


async def test_get_session_builds_session_without_connecting(
    monkeypatch: pytest.MonkeyPatch, fresh_caches: None
) -> None:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", "postgresql://u:p@localhost:1/db")
    assert get_engine() is get_engine()
    async with get_session() as session:
        assert session.bind is get_engine()
