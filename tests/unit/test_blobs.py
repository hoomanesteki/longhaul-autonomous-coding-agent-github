"""Blob store behaviour that needs no network: key rules, the memory store, the S3 wrapper.

The S3 wrapper runs against botocore's Stubber, so its error paths (a missing key, an object
of the wrong length) are covered without MinIO. The real round trip is in
tests/integration/test_blobs.py.
"""

import base64
import hashlib
import io

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import Stubber
from pydantic import BaseModel, SecretStr, ValidationError

from longhaul.blobs.base import (
    BlobError,
    BlobIntegrityError,
    BlobNotFoundError,
    blob_key,
    check_key,
)
from longhaul.blobs.memory import MemoryBlobStore
from longhaul.blobs.s3 import S3BlobStore
from longhaul.contracts import BlobRef, Message, TextBlock, ToolResultBlock, ToolUseBlock
from longhaul.settings import EnvSettings

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


class Note(BaseModel):
    text: str
    tags: list[str] = []


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- blob_key -------------------------------------------------------------------------------


def test_blob_key_follows_the_documented_layout() -> None:
    assert blob_key("lh-x", "turns", 7, "response.json") == "tasks/lh-x/turns/0007/response.json"


@pytest.mark.parametrize(
    ("turn_index", "padded"), [(0, "0000"), (12, "0012"), (9999, "9999"), (10000, "10000")]
)
def test_blob_key_pads_the_turn_index_so_keys_sort_by_turn(turn_index: int, padded: str) -> None:
    assert blob_key("lh-x", "turns", turn_index, "a.json") == f"tasks/lh-x/turns/{padded}/a.json"


def test_blob_key_accepts_the_characters_of_real_task_ids() -> None:
    # Task ids are lh-<owner>-<repo>-<issue>, and repo names may hold dots and underscores.
    key = blob_key("lh-acme-my_repo.v2-42", "turns", 1, "out_toolu_01AbC.txt")
    assert key == "tasks/lh-acme-my_repo.v2-42/turns/0001/out_toolu_01AbC.txt"


BAD_PARTS = [
    "",
    ".",
    "..",
    "../x",
    "a/../b",
    "/abs",
    "a/b",
    "a\\b",
    "a b",
    "a\x00b",
    "a\nb",
    "a..b",
    ".hidden",
    "-flag",
    "café",
]


@pytest.mark.parametrize("bad", BAD_PARTS)
@pytest.mark.parametrize("position", ["task_id", "kind", "name"])
def test_blob_key_rejects_unsafe_parts(position: str, bad: str) -> None:
    parts = {"task_id": "lh-x", "kind": "turns", "name": "response.json", position: bad}
    with pytest.raises(ValueError, match="blob key"):
        blob_key(parts["task_id"], parts["kind"], 3, parts["name"])


def test_blob_key_rejects_a_negative_turn_index() -> None:
    with pytest.raises(ValueError, match="blob key"):
        blob_key("lh-x", "turns", -1, "a.json")


# --- check_key: what put and get accept ------------------------------------------------------


UNSAFE_KEYS = [
    "",
    "/abs/x",
    "..",
    "a/../b",
    "a/..",
    "a..b",
    "a//b",
    "a/",
    "./a",
    "a/./b",
    "a\\b",
    "a\x00b",
    "a\nb",
    "x" * 1025,
]


@pytest.mark.parametrize("key", UNSAFE_KEYS)
def test_check_key_rejects_keys_that_could_escape_or_confuse(key: str) -> None:
    with pytest.raises(ValueError, match="blob key"):
        check_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "a",
        "tasks/lh-x/turns/0007/response.json",
        "tasks/lh-x/ckpt/0123abc.bundle",
        "x" * 1024,
    ],
)
def test_check_key_accepts_ordinary_keys(key: str) -> None:
    assert check_key(key) == key


# --- MemoryBlobStore ------------------------------------------------------------------------


async def test_memory_store_round_trips_and_describes_what_it_stored() -> None:
    store = MemoryBlobStore()
    data = b"hello \x00 world"
    ref = await store.put(data, key="tasks/lh-x/turns/0001/a.bin")
    assert ref == BlobRef(key="tasks/lh-x/turns/0001/a.bin", sha256=_sha(data), size=len(data))
    assert await store.get(ref) == data


async def test_memory_store_handles_empty_data() -> None:
    store = MemoryBlobStore()
    ref = await store.put(b"", key="empty")
    assert ref.size == 0
    assert await store.get(ref) == b""


async def test_memory_store_rejects_same_size_content_with_another_hash() -> None:
    store = MemoryBlobStore()
    ref = await store.put(b"original", key="k")
    store.objects["k"] = b"tampered"
    with pytest.raises(BlobIntegrityError, match="sha256"):
        await store.get(ref)


@pytest.mark.parametrize("replacement", [b"much longer than before", b"short", b""])
async def test_memory_store_rejects_content_of_another_size(replacement: bytes) -> None:
    store = MemoryBlobStore()
    ref = await store.put(b"original", key="k")
    store.objects["k"] = replacement
    with pytest.raises(BlobIntegrityError, match="bytes"):
        await store.get(ref)


async def test_memory_store_raises_not_found_for_a_missing_key() -> None:
    ref = BlobRef(key="nowhere", sha256=_sha(b""), size=0)
    with pytest.raises(BlobNotFoundError, match="nowhere"):
        await MemoryBlobStore().get(ref)


def test_both_failures_share_one_base_class() -> None:
    # A caller that only wants to know "the blob could not be used" catches BlobError.
    assert issubclass(BlobIntegrityError, BlobError)
    assert issubclass(BlobNotFoundError, BlobError)


async def test_putting_a_key_again_replaces_the_object_and_the_old_ref_fails() -> None:
    # A retried activity writes the same key. The new ref is good, a stale ref is not.
    store = MemoryBlobStore()
    old = await store.put(b"first answer", key="k")
    new = await store.put(b"second answer", key="k")
    assert await store.get(new) == b"second answer"
    with pytest.raises(BlobIntegrityError):
        await store.get(old)


async def test_putting_the_same_bytes_again_returns_the_same_ref() -> None:
    store = MemoryBlobStore()
    assert await store.put(b"same", key="k") == await store.put(b"same", key="k")


@pytest.mark.parametrize("key", UNSAFE_KEYS)
async def test_memory_store_refuses_unsafe_keys_on_put_and_get(key: str) -> None:
    store = MemoryBlobStore()
    with pytest.raises(ValueError, match="blob key"):
        await store.put(b"x", key=key)
    with pytest.raises(ValueError, match="blob key"):
        await store.get(BlobRef(key=key, sha256=_sha(b"x"), size=1))


async def test_memory_store_keeps_its_own_copy_of_the_data() -> None:
    store = MemoryBlobStore()
    data = bytearray(b"abc")
    ref = await store.put(bytes(data), key="k")
    data[0] = 0
    assert await store.get(ref) == b"abc"


# --- JSON helpers ---------------------------------------------------------------------------


async def test_json_helpers_round_trip_a_model() -> None:
    store = MemoryBlobStore()
    note = Note(text="café", tags=["a", "b"])
    ref = await store.put_json(note, "notes/one.json")
    assert ref.key == "notes/one.json"
    assert await store.get_json(ref, Note) == note


async def test_json_helpers_round_trip_a_transcript() -> None:
    # CarryState.transcript_ref points at list[Message] JSON, so lists must work too.
    store = MemoryBlobStore()
    transcript = [
        Message(role="user", content=[TextBlock(text="fix the bug")]),
        Message(
            role="assistant",
            content=[
                TextBlock(text="reading"),
                ToolUseBlock(id="t1", name="read_file", input={"path": "calc.py"}),
            ],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="def add(): ...")]),
    ]
    ref = await store.put_json(transcript, "tasks/lh-x/transcript.json")
    assert await store.get_json(ref, list[Message]) == transcript


async def test_get_json_reports_content_that_does_not_fit_the_model() -> None:
    store = MemoryBlobStore()
    ref = await store.put(b'{"unexpected": 1}', key="notes/bad.json")
    with pytest.raises(ValidationError):
        await store.get_json(ref, Note)


async def test_get_json_checks_integrity_before_parsing() -> None:
    store = MemoryBlobStore()
    ref = await store.put_json(Note(text="a"), "notes/one.json")
    store.objects["notes/one.json"] = Note(text="b").model_dump_json().encode()
    with pytest.raises(BlobIntegrityError):
        await store.get_json(ref, Note)


# --- S3BlobStore against botocore's Stubber ---------------------------------------------------


def _s3() -> S3BlobStore:
    # Nothing connects: the Stubber answers every call before a request is sent.
    return S3BlobStore("http://localhost:1", "bucket", "AKIDEXAMPLE", "secret")


def _body(data: bytes, *, length: int | None = None) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data) if length is None else length)


async def test_s3_put_sends_the_sha256_so_the_server_can_check_the_upload() -> None:
    store = _s3()
    data = b"payload"
    digest = hashlib.sha256(data)
    expected = {
        "Bucket": "bucket",
        "Key": "tasks/lh-x/a.bin",
        "Body": data,
        "ChecksumSHA256": base64.b64encode(digest.digest()).decode(),
    }
    with Stubber(store._client) as stub:
        stub.add_response("put_object", {}, expected)
        ref = await store.put(data, key="tasks/lh-x/a.bin")
        stub.assert_no_pending_responses()
    assert ref == BlobRef(key="tasks/lh-x/a.bin", sha256=digest.hexdigest(), size=len(data))


async def test_s3_get_returns_verified_bytes() -> None:
    store = _s3()
    data = b"stored bytes"
    ref = BlobRef(key="k/a.bin", sha256=_sha(data), size=len(data))
    with Stubber(store._client) as stub:
        stub.add_response(
            "get_object",
            {"Body": _body(data), "ContentLength": len(data)},
            {"Bucket": "bucket", "Key": "k/a.bin"},
        )
        assert await store.get(ref) == data


async def test_s3_get_rejects_content_with_another_hash() -> None:
    store = _s3()
    ref = BlobRef(key="k/a.bin", sha256=_sha(b"hello"), size=5)
    with Stubber(store._client) as stub:
        stub.add_response("get_object", {"Body": _body(b"jello"), "ContentLength": 5})
        with pytest.raises(BlobIntegrityError, match="sha256"):
            await store.get(ref)


async def test_s3_get_refuses_a_wrong_length_before_reading_the_body() -> None:
    # A replaced object could be far larger than the ref says. It must not be loaded.
    class Unreadable(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise AssertionError("the body was read")

    store = _s3()
    ref = BlobRef(key="k/a.bin", sha256=_sha(b"abc"), size=3)
    body = StreamingBody(Unreadable(), 9_999)
    with Stubber(store._client) as stub:
        stub.add_response("get_object", {"Body": body, "ContentLength": 9_999})
        with pytest.raises(BlobIntegrityError, match="9999 bytes"):
            await store.get(ref)


async def test_s3_get_maps_a_missing_key_to_not_found() -> None:
    store = _s3()
    ref = BlobRef(key="k/missing.bin", sha256=_sha(b""), size=0)
    with Stubber(store._client) as stub:
        stub.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
        with pytest.raises(BlobNotFoundError, match="k/missing.bin"):
            await store.get(ref)


async def test_s3_get_lets_other_server_errors_through() -> None:
    # Access denied is a deployment problem, not a missing blob, so it must stay visible.
    store = _s3()
    ref = BlobRef(key="k/a.bin", sha256=_sha(b""), size=0)
    with Stubber(store._client) as stub:
        stub.add_client_error("get_object", service_error_code="AccessDenied", http_status_code=403)
        with pytest.raises(ClientError):
            await store.get(ref)


async def test_s3_store_refuses_unsafe_keys_before_any_request() -> None:
    store = _s3()
    with Stubber(store._client) as stub:  # no responses queued: any request would fail
        with pytest.raises(ValueError, match="blob key"):
            await store.put(b"x", key="../escape")
        with pytest.raises(ValueError, match="blob key"):
            await store.get(BlobRef(key="/abs", sha256=_sha(b"x"), size=1))
        stub.assert_no_pending_responses()


# --- S3BlobStore.from_settings ----------------------------------------------------------------


def _settings(**fields: str | SecretStr | None) -> EnvSettings:
    # _env_file=None keeps a developer's .env out of the test.
    return EnvSettings(_env_file=None, **fields)  # type: ignore[call-arg]


def test_from_settings_uses_the_configured_endpoint_bucket_and_keys() -> None:
    store = S3BlobStore.from_settings(
        _settings(
            blob_endpoint="http://minio.test:9000",
            blob_bucket="evidence",
            blob_access_key=SecretStr("AKIDEXAMPLE"),
            blob_secret_key=SecretStr("not-a-real-secret"),
        )
    )
    # A presigned URL is built offline from the client's endpoint, addressing and credentials.
    url = store._client.generate_presigned_url(
        "get_object", Params={"Bucket": "evidence", "Key": "k"}
    )
    assert url.startswith("http://minio.test:9000/evidence/k?")
    assert "AKIDEXAMPLE" in url


def test_from_settings_names_every_missing_variable() -> None:
    settings = _settings(
        blob_endpoint=None,
        blob_bucket="evidence",
        blob_access_key=None,
        blob_secret_key=SecretStr("s"),
    )
    with pytest.raises(RuntimeError) as caught:
        S3BlobStore.from_settings(settings)
    message = str(caught.value)
    assert "LONGHAUL_BLOB_ENDPOINT" in message
    assert "LONGHAUL_BLOB_ACCESS_KEY" in message
    assert "LONGHAUL_BLOB_BUCKET" not in message
    assert "LONGHAUL_BLOB_SECRET_KEY" not in message


def test_from_settings_does_not_put_secrets_in_the_error() -> None:
    settings = _settings(blob_endpoint=None, blob_secret_key=SecretStr("hunter2-secret"))
    with pytest.raises(RuntimeError) as caught:
        S3BlobStore.from_settings(settings)
    assert "hunter2" not in str(caught.value)
