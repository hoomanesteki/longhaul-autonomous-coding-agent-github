"""Blob store against a real S3 API (MinIO from the compose stack).

Every test writes under its own random task prefix and deletes it afterwards, so tests can run
in any order and leave the bucket as they found it. Tampering goes through a separate boto3
client, because the store under test must not be the one that vouches for its own objects.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3
import pytest
from botocore.config import Config
from pydantic import BaseModel, SecretStr

from longhaul.blobs.base import BlobIntegrityError, BlobNotFoundError, blob_key
from longhaul.blobs.s3 import S3BlobStore
from longhaul.contracts import BlobRef, Message, TextBlock, ToolUseBlock
from longhaul.settings import EnvSettings

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

pytestmark = [pytest.mark.integration, pytest.mark.phase2]

LARGE = 5 * 1024 * 1024


class Note(BaseModel):
    text: str
    numbers: list[int]


@dataclass(frozen=True)
class Coordinates:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str


@dataclass(frozen=True)
class Scratch:
    """The part of the bucket one test may use: tasks/<task_id>/."""

    task_id: str

    @property
    def prefix(self) -> str:
        return f"tasks/{self.task_id}"

    def key(self, name: str) -> str:
        return f"{self.prefix}/{name}"


@pytest.fixture
def where() -> Coordinates:
    """Where the stack's MinIO is. LONGHAUL_BLOB_* wins and the compose defaults fill gaps."""
    settings = EnvSettings()
    return Coordinates(
        endpoint=settings.blob_endpoint or "http://localhost:9000",
        bucket=settings.blob_bucket or "longhaul",
        access_key=settings.blob_access_key.get_secret_value()
        if settings.blob_access_key
        else "longhaul",
        secret_key=settings.blob_secret_key.get_secret_value()
        if settings.blob_secret_key
        else "longhaul-secret",
    )


@pytest.fixture
def store(where: Coordinates) -> S3BlobStore:
    return S3BlobStore(where.endpoint, where.bucket, where.access_key, where.secret_key)


@pytest.fixture
def s3(where: Coordinates) -> S3Client:
    """A plain boto3 client for looking at, and changing, objects behind the store's back."""
    return boto3.client(
        "s3",
        endpoint_url=where.endpoint,
        aws_access_key_id=where.access_key,
        aws_secret_access_key=where.secret_key,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


@pytest.fixture
def scratch(where: Coordinates, s3: S3Client) -> Iterator[Scratch]:
    area = Scratch(task_id=f"lh-202-{uuid.uuid4().hex[:12]}")
    yield area
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=where.bucket, Prefix=area.prefix)
    for page in pages:
        for item in page.get("Contents", []):
            s3.delete_object(Bucket=where.bucket, Key=item["Key"])


async def test_roundtrip_large(
    store: S3BlobStore, s3: S3Client, where: Coordinates, scratch: Scratch
) -> None:
    data = os.urandom(LARGE)

    ref = await store.put(data, key=scratch.key("large.bin"))

    assert ref == BlobRef(
        key=scratch.key("large.bin"), sha256=hashlib.sha256(data).hexdigest(), size=LARGE
    )
    assert s3.head_object(Bucket=where.bucket, Key=ref.key)["ContentLength"] == LARGE
    assert await store.get(ref) == data


@pytest.mark.parametrize(
    ("tampered", "complaint"),
    [
        (b"x" * 64, "sha256"),  # same length, different bytes
        (b"x" * 65, "bytes"),  # one byte longer
        (b"x" * 10, "bytes"),  # shorter
        (b"", "bytes"),  # emptied
    ],
    ids=["same-length", "longer", "shorter", "empty"],
)
async def test_integrity_check(
    store: S3BlobStore,
    s3: S3Client,
    where: Coordinates,
    scratch: Scratch,
    tampered: bytes,
    complaint: str,
) -> None:
    ref = await store.put(b"y" * 64, key=scratch.key("response.json"))

    s3.put_object(Bucket=where.bucket, Key=ref.key, Body=tampered)

    with pytest.raises(BlobIntegrityError, match=complaint):
        await store.get(ref)


async def test_tampering_with_one_object_leaves_others_readable(
    store: S3BlobStore, s3: S3Client, where: Coordinates, scratch: Scratch
) -> None:
    good = await store.put(b"good", key=scratch.key("good.bin"))
    bad = await store.put(b"bad", key=scratch.key("bad.bin"))
    s3.put_object(Bucket=where.bucket, Key=bad.key, Body=b"evil")

    assert await store.get(good) == b"good"
    with pytest.raises(BlobIntegrityError):
        await store.get(bad)


async def test_json_roundtrip(store: S3BlobStore, scratch: Scratch) -> None:
    note = Note(text="café €", numbers=[1, 2, 3])
    note_ref = await store.put_json(note, scratch.key("note.json"))
    assert await store.get_json(note_ref, Note) == note

    transcript = [
        Message(role="user", content=[TextBlock(text="fix the bug")]),
        Message(
            role="assistant",
            content=[ToolUseBlock(id="t1", name="read_file", input={"path": "calc.py"})],
        ),
    ]
    key = blob_key(scratch.task_id, "turns", 7, "response.json")
    assert key == scratch.key("turns/0007/response.json")
    transcript_ref = await store.put_json(transcript, key)
    assert await store.get_json(transcript_ref, list[Message]) == transcript


async def test_a_missing_object_is_reported_as_not_found(
    store: S3BlobStore, scratch: Scratch
) -> None:
    ref = BlobRef(
        key=scratch.key("never-written.bin"), sha256=hashlib.sha256(b"").hexdigest(), size=0
    )
    with pytest.raises(BlobNotFoundError, match="never-written"):
        await store.get(ref)


async def test_putting_a_key_again_replaces_the_object(
    store: S3BlobStore, scratch: Scratch
) -> None:
    # A retried activity writes the same key. The new ref is good and the old one is not.
    key = scratch.key("retried.json")
    old = await store.put(b"first attempt", key=key)
    new = await store.put(b"second attempt", key=key)

    assert await store.get(new) == b"second attempt"
    with pytest.raises(BlobIntegrityError):
        await store.get(old)


async def test_many_calls_at_once_share_one_client(store: S3BlobStore, scratch: Scratch) -> None:
    # Calls run on worker threads. boto3 clients are thread-safe, and this keeps it so.
    payloads = {scratch.key(f"n{i:02d}.bin"): os.urandom(64 * 1024) for i in range(24)}

    refs = await asyncio.gather(*(store.put(data, key=key) for key, data in payloads.items()))
    fetched = await asyncio.gather(*(store.get(ref) for ref in refs))

    assert dict(zip(payloads, fetched, strict=True)) == payloads


async def test_from_settings_builds_a_working_store(where: Coordinates, scratch: Scratch) -> None:
    store = S3BlobStore.from_settings(
        EnvSettings(
            blob_endpoint=where.endpoint,
            blob_bucket=where.bucket,
            blob_access_key=SecretStr(where.access_key),
            blob_secret_key=SecretStr(where.secret_key),
        )
    )

    ref = await store.put(b"configured", key=scratch.key("configured.bin"))

    assert await store.get(ref) == b"configured"
