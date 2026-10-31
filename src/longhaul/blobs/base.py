"""What every blob store shares: errors, key rules, hash checks and the JSON helpers.

S3BlobStore and MemoryBlobStore both build on BlobStoreBase, so the memory store that unit
tests use applies the same checks as the real one.
"""

from __future__ import annotations

import hashlib
import re
from abc import abstractmethod
from collections.abc import Sequence

from pydantic import BaseModel, TypeAdapter
from pydantic_core import to_json

from longhaul.contracts import BlobRef, BlobStore

# S3 refuses keys longer than this many bytes of UTF-8.
MAX_KEY_BYTES = 1024

# One piece of a canonical key. It starts with a letter or digit, so a piece can never be
# "." or "..", or look like a command line flag when someone pastes a key into a shell.
_PART = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class BlobError(Exception):
    """A stored blob could not be used."""


class BlobIntegrityError(BlobError):
    """The bytes in the store do not match the BlobRef that points at them."""


class BlobNotFoundError(BlobError):
    """No object exists under the ref's key."""


def check_key(key: str) -> str:
    """Return key if it is safe to use as an object key, else raise ValueError.

    Any ".." is refused, not only a ".." path segment. That is stricter than traversal needs,
    but it means a sanitizer that strips dots, or a client that normalizes paths, cannot turn
    an accepted key into a different one.
    """
    problem = _key_problem(key)
    if problem:
        raise ValueError(f"invalid blob key {key[:80]!r}: {problem}")
    return key


def _key_problem(key: str) -> str | None:
    if not key:
        return "it is empty"
    if len(key.encode()) > MAX_KEY_BYTES:
        return f"it is longer than {MAX_KEY_BYTES} bytes"
    if ".." in key:
        return "it contains '..'"
    if any(ord(char) < 32 or char in "\x7f\\" for char in key):
        return "it contains a control character or a backslash"
    segments = key.split("/")
    if "" in segments:
        return "it starts or ends with '/' or has an empty segment"
    if "." in segments:
        return "it has a '.' segment"
    return None


def blob_key(task_id: str, kind: str, turn_index: int, name: str) -> str:
    """The key of one blob of one turn, such as tasks/lh-x/turns/0007/response.json.

    The turn index is zero padded so that a listing sorts by turn.
    """
    if turn_index < 0:
        raise ValueError(f"invalid blob key: turn_index must not be negative, got {turn_index}")
    for label, part in (("task_id", task_id), ("kind", kind), ("name", name)):
        if not _PART.fullmatch(part) or ".." in part:
            raise ValueError(
                f"invalid blob key {label} {part[:80]!r}: use letters, digits, '.', '_' and '-', "
                "start with a letter or digit, and never use '..'"
            )
    return check_key(f"tasks/{task_id}/{kind}/{turn_index:04d}/{name}")


def make_ref(data: bytes, key: str) -> BlobRef:
    """Describe data stored under key. Every BlobRef a store returns comes from here."""
    return BlobRef(key=check_key(key), sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def check_size(ref: BlobRef, length: int) -> None:
    if length != ref.size:
        raise BlobIntegrityError(f"blob {ref.key} holds {length} bytes, the ref says {ref.size}")


def verify(ref: BlobRef, data: bytes) -> bytes:
    """Return data if it is exactly what ref describes, else raise BlobIntegrityError."""
    check_size(ref, len(data))
    digest = hashlib.sha256(data).hexdigest()
    if digest != ref.sha256:
        raise BlobIntegrityError(f"blob {ref.key} has sha256 {digest}, the ref says {ref.sha256}")
    return data


class BlobStoreBase(BlobStore):
    """The put and get of contracts.BlobStore, plus JSON helpers that both stores share.

    Naming the protocol as a base makes mypy check every store against it.
    """

    @abstractmethod
    async def put(self, data: bytes, *, key: str) -> BlobRef:
        """Store data under key and return a ref that get can verify. A key is overwritten."""

    @abstractmethod
    async def get(self, ref: BlobRef) -> bytes:
        """Return the bytes ref describes, or raise BlobNotFoundError or BlobIntegrityError."""

    async def put_json(self, model: BaseModel | Sequence[BaseModel], key: str) -> BlobRef:
        """Store a pydantic model, or a list of them such as a transcript, as JSON."""
        return await self.put(to_json(model), key=key)

    async def get_json[T](self, ref: BlobRef, model: type[T]) -> T:
        """Load JSON written by put_json. The integrity check runs before parsing."""
        return TypeAdapter(model).validate_json(await self.get(ref))
