"""In-memory blob store for unit tests. It applies the same checks as S3BlobStore."""

from __future__ import annotations

from longhaul.blobs.base import BlobNotFoundError, BlobStoreBase, check_key, make_ref, verify
from longhaul.contracts import BlobRef


class MemoryBlobStore(BlobStoreBase):
    def __init__(self) -> None:
        # Public so that a test can replace an object to simulate tampering.
        self.objects: dict[str, bytes] = {}

    async def put(self, data: bytes, *, key: str) -> BlobRef:
        ref = make_ref(data, key)
        self.objects[key] = bytes(data)
        return ref

    async def get(self, ref: BlobRef) -> bytes:
        check_key(ref.key)
        try:
            data = self.objects[ref.key]
        except KeyError:
            raise BlobNotFoundError(f"no blob at {ref.key}") from None
        return verify(ref, data)
