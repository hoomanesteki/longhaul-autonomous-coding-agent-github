import os

import boto3
import psycopg
import pytest
from temporalio.client import Client

pytestmark = [pytest.mark.integration, pytest.mark.phase0]


async def test_services_reachable() -> None:
    dsn = os.environ.get(
        "LONGHAUL_DATABASE_URL", "postgresql://longhaul:longhaul@localhost:5432/longhaul"
    )
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        row = conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
        assert row is not None

    client = await Client.connect(
        os.environ.get("LONGHAUL_TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("LONGHAUL_TEMPORAL_NAMESPACE", "default"),
    )
    assert client.namespace

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("LONGHAUL_BLOB_ENDPOINT", "http://localhost:9000"),
        aws_access_key_id=os.environ.get("LONGHAUL_BLOB_ACCESS_KEY", "longhaul"),
        aws_secret_access_key=os.environ.get("LONGHAUL_BLOB_SECRET_KEY", "longhaul-secret"),
        region_name="us-east-1",
    )
    bucket = os.environ.get("LONGHAUL_BLOB_BUCKET", "longhaul")
    names = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert bucket in names
