import asyncio
from collections.abc import Iterator

import boto3
import psycopg
import pytest
from sqlalchemy import text
from temporalio.client import Client

from longhaul.db.session import dispose_engine, get_engine, get_session
from longhaul.settings import EnvSettings, get_settings

pytestmark = [pytest.mark.integration, pytest.mark.phase0]

LOCAL_DSN = "postgresql://longhaul:longhaul@localhost:5432/longhaul"


# Settings come from EnvSettings, so a .env that remaps a port (see docs/runbook.md) applies
# to these tests the same way it applies to the worker and to make migrate.
def _database_url() -> str:
    secret = EnvSettings().database_url
    return secret.get_secret_value() if secret else LOCAL_DSN


async def test_services_reachable() -> None:
    settings = EnvSettings()
    with psycopg.connect(_database_url(), autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
        assert row is not None

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    assert client.namespace

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.blob_endpoint or "http://localhost:9000",
        aws_access_key_id=(
            settings.blob_access_key.get_secret_value() if settings.blob_access_key else "longhaul"
        ),
        aws_secret_access_key=(
            settings.blob_secret_key.get_secret_value()
            if settings.blob_secret_key
            else "longhaul-secret"
        ),
        region_name="us-east-1",
    )
    bucket = settings.blob_bucket or "longhaul"
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert bucket in names


@pytest.fixture
def application_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("LONGHAUL_DATABASE_URL", _database_url())
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()


def test_sessions_work_on_separate_event_loops(application_database: None) -> None:
    """A CLI that calls asyncio.run twice, or tests that each get their own loop.

    Pooled asyncpg connections belong to the loop that opened them. Without dispose_engine
    the second loop would check out a connection from a closed one and fail.
    """

    async def select_one() -> int:
        try:
            async with get_session() as session:
                return int((await session.execute(text("select 1"))).scalar_one())
        finally:
            await dispose_engine()

    assert asyncio.run(select_one()) == 1
    assert asyncio.run(select_one()) == 1
