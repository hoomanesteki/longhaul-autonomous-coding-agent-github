"""S3-compatible blob store: MinIO in the local stack, S3 in AWS.

boto3 runs on worker threads through asyncio.to_thread instead of aioboto3. The reasons are in
ADR-0005.
"""

from __future__ import annotations

import asyncio
import base64

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from longhaul.blobs.base import (
    BlobIntegrityError,
    BlobNotFoundError,
    BlobStoreBase,
    blob_key,
    check_key,
    check_size,
    make_ref,
    verify,
)
from longhaul.contracts import BlobRef
from longhaul.settings import EnvSettings

# The errors and blob_key are defined in base.py so that the memory store can share them
# without importing boto3. They are listed here because the plan places them with the S3 store.
__all__ = ["BlobIntegrityError", "BlobNotFoundError", "S3BlobStore", "blob_key"]

# GetObject answers a missing key with NoSuchKey. The other two are what a HEAD would say.
_MISSING = frozenset({"NoSuchKey", "NotFound", "404"})

_CLIENT_CONFIG = Config(
    signature_version="s3v4",
    # MinIO serves buckets by path. Virtual-host addressing would need a wildcard DNS name.
    s3={"addressing_style": "path"},
    # A dead endpoint has to fail in seconds, not hold a worker thread. Temporal retries the
    # activity if the blob matters.
    connect_timeout=5,
    read_timeout=60,
    retries={"mode": "standard", "max_attempts": 3},
    # Room for every thread of asyncio's default executor, so parallel calls do not queue.
    max_pool_connections=32,
)


class S3BlobStore(BlobStoreBase):
    """Objects in one bucket, under the keys callers choose (see blob_key).

    The bucket must exist: the store does not create it, and it never deletes anything.
    """

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
    ) -> None:
        self._bucket = bucket
        # One client for the life of the store. boto3 clients are thread-safe and slow to build.
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=_CLIENT_CONFIG,
        )

    @classmethod
    def from_settings(cls, settings: EnvSettings) -> S3BlobStore:
        """Build the store from LONGHAUL_BLOB_* settings.

        There is no default endpoint. Falling back to localhost could write a task's blobs to a
        different store than the one the operator meant.
        """
        endpoint, bucket = settings.blob_endpoint, settings.blob_bucket
        access_key, secret_key = settings.blob_access_key, settings.blob_secret_key
        if endpoint is None or bucket is None or access_key is None or secret_key is None:
            unset = [
                name
                for name, value in (
                    ("LONGHAUL_BLOB_ENDPOINT", endpoint),
                    ("LONGHAUL_BLOB_BUCKET", bucket),
                    ("LONGHAUL_BLOB_ACCESS_KEY", access_key),
                    ("LONGHAUL_BLOB_SECRET_KEY", secret_key),
                )
                if value is None
            ]
            raise RuntimeError("the blob store is not configured; set " + ", ".join(unset))
        return cls(endpoint, bucket, access_key.get_secret_value(), secret_key.get_secret_value())

    async def put(self, data: bytes, *, key: str) -> BlobRef:
        return await asyncio.to_thread(self._put, data, key)

    async def get(self, ref: BlobRef) -> bytes:
        return await asyncio.to_thread(self._get, ref)

    # Hashing and the transfer both block, so the whole of each runs on a worker thread.
    def _put(self, data: bytes, key: str) -> BlobRef:
        ref = make_ref(data, key)
        # The server recomputes the sha256 and rejects the upload on a mismatch. A ref that put
        # returns therefore describes what is stored, and damage in transit shows up here
        # instead of at the first read, which may be a replay days later.
        checksum = base64.b64encode(bytes.fromhex(ref.sha256)).decode("ascii")
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ChecksumSHA256=checksum)
        return ref

    def _get(self, ref: BlobRef) -> bytes:
        check_key(ref.key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=ref.key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _MISSING:
                raise BlobNotFoundError(f"no blob at {ref.key}") from exc
            raise
        body = response["Body"]
        try:
            # An object that was replaced can be far larger than the ref says. Compare the
            # length before reading so that it is never loaded into memory.
            length = response.get("ContentLength")
            if length is not None:
                check_size(ref, length)
            return verify(ref, body.read())
        finally:
            body.close()
