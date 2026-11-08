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

# A name made from a tool call id is at most this long. The key as a whole may be 1,024 bytes.
_SLUG_MAX = 80
_SAFE_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


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
        _require_part(label, part)
    return check_key(f"tasks/{task_id}/{kind}/{turn_index:04d}/{name}")


def _require_part(label: str, part: str) -> None:
    if not _PART.fullmatch(part) or ".." in part:
        raise ValueError(
            f"invalid blob key {label} {part[:80]!r}: use letters, digits, '.', '_' and '-', "
            "start with a letter or digit, and never use '..'"
        )


def call_slug(call_id: str) -> str:
    """A tool id as a part of a blob name.

    Anthropic and OpenAI ids are letters, digits and underscores and stay as they are. A
    provider behind an OpenAI-compatible server can send other characters, such as a colon or a
    slash, and blob_key refuses those. They become underscores, and a short hash of the original
    keeps two such ids from sharing a name.
    """
    if _SAFE_SLUG.fullmatch(call_id) and ".." not in call_id and len(call_id) <= _SLUG_MAX:
        return call_id
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", call_id).replace("..", "__")
    cleaned = cleaned.lstrip("._-")[: _SLUG_MAX - 11] or "call"
    return f"{cleaned}-{hashlib.sha256(call_id.encode()).hexdigest()[:10]}"


def handle_key(task_id: str, call_id: str) -> str:
    """The key of the index blob that says where the full output of one tool call is stored.

    An output handle is `out_<call id>`. The blob that holds the text sits under the turn that
    made the call (blob_key), and the handle does not say which turn that was. So the text is
    found in two steps, both from the task and the call alone and neither from the database:
    this key holds a small JSON BlobRef, and the ref names the text. A call id with characters
    that a key refuses is cleaned by call_slug, so no handle can reach outside its task.
    """
    _require_part("task_id", task_id)
    return check_key(f"tasks/{task_id}/outputs/{call_slug(call_id)}.json")


def report_key(task_id: str) -> str:
    """The key of the run report of a task (LH-705), such as tasks/lh-x/report/run-report.md.

    A task has one report, so the key has no turn in it. A newer report replaces the older one.
    """
    _require_part("task_id", task_id)
    return check_key(f"tasks/{task_id}/report/run-report.md")


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

    @abstractmethod
    async def get_by_key(self, key: str) -> bytes:
        """Return the bytes stored under key, or raise BlobNotFoundError.

        There is no ref, so nothing checks the bytes. It is for a blob whose key is worked out
        from known names and whose content is a pointer that get() then verifies, such as the
        index of an output handle (handle_key). Use get() whenever a ref exists.
        """

    async def put_json(self, model: BaseModel | Sequence[BaseModel], key: str) -> BlobRef:
        """Store a pydantic model, or a list of them such as a transcript, as JSON."""
        return await self.put(to_json(model), key=key)

    async def get_json[T](self, ref: BlobRef, model: type[T]) -> T:
        """Load JSON written by put_json. The integrity check runs before parsing."""
        return TypeAdapter(model).validate_json(await self.get(ref))
