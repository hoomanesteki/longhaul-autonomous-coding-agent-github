import pytest

from longhaul.db.session import make_engine, to_async_url

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
