"""Text embedders for the memory store (LH-601, ADR-0010).

The memory store finds lessons by meaning: it embeds the issue text and asks Postgres for the
stored items whose vectors lie closest by cosine distance. This module makes the vectors. Both
embedders return vectors of unit length and EMBEDDING_DIM numbers, which is the width of the
`memory_items.embedding` column. For unit vectors the cosine distance is one minus the dot
product, so the two agree on what "close" means.

* LocalEmbedder runs BAAI/bge-small-en-v1.5 on this machine through fastembed, so retrieval
  works offline and needs no API key. fastembed is an optional extra (`uv sync --extra memory`)
  and is imported when the first text is embedded, so a worker that never searches memory does
  not load onnxruntime.
* HashEmbedder is the hashing trick: each word lands in one of EMBEDDING_DIM buckets with a
  sign, and the counts are scaled to unit length. It knows nothing about meaning, only about
  shared words, which is enough for tests and CI to check that the table finds the nearest
  neighbour. It needs no download and gives the same vector in every process.

The vectors of the two are not comparable. A database holds the vectors of one embedder, and
changing the embedder means embedding every row again (ADR-0010).
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import unicodedata
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastembed import TextEmbedding

__all__ = ["EMBEDDING_DIM", "LOCAL_MODEL", "HashEmbedder", "LocalEmbedder"]

# The width of every stored vector: bge-small-en-v1.5 makes 384 numbers per text, and the
# memory_items.embedding column is vector(384).
EMBEDDING_DIM = 384
LOCAL_MODEL = "BAAI/bge-small-en-v1.5"

# Mixed into every word hash. It names the scheme, so a later change to how words are bucketed
# takes a new key and cannot be mistaken for this one.
_HASH_KEY = b"longhaul-hash-embedder-v1"
# A word is a run of letters and digits. The underscore is left out, so that remove_item gives
# the same words as "remove item".
_WORD = re.compile(r"[^\W_]+")


def _unit(values: Sequence[float]) -> list[float]:
    """Scale `values` to length 1."""
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0:
        raise ValueError("a vector of length zero cannot be scaled to unit length")
    return [v / norm for v in values]


def _bucket(word: str, dim: int) -> tuple[int, int]:
    """The bucket of a word and its sign, +1 or -1.

    The hash is blake2b with a fixed key. Python's own hash() of a str changes from one process
    to the next, which would give a restarted worker other vectors than the ones in the table.
    Four bytes of the digest pick the bucket and one bit of another byte picks the sign, so the
    two are independent. With signs, two unrelated words that share a bucket cancel as often as
    they add up, and the dot product of unrelated texts stays near zero.
    """
    digest = hashlib.blake2b(word.encode(), digest_size=8, key=_HASH_KEY).digest()
    return int.from_bytes(digest[:4], "big") % dim, 1 if digest[4] & 1 else -1


def _counts(words: Sequence[str], dim: int) -> list[int]:
    counts = [0] * dim
    for word, times in Counter(words).items():
        bucket, sign = _bucket(word, dim)
        counts[bucket] += sign * times
    return counts


class HashEmbedder:
    """Word counts hashed into `dim` signed buckets and scaled to unit length.

    Case, punctuation and word order do not matter, and look-alike forms such as full-width
    letters are folded into plain ones (NFKC) before the words are cut out.
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        if dim < 1:
            raise ValueError(f"dim must be a positive number of buckets, got {dim}")
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        folded = unicodedata.normalize("NFKC", text).casefold()
        words = _WORD.findall(folded)
        counts = _counts(words, self.dim)
        if not any(counts):
            # No words, or words whose signs cancelled each other. The zero vector has no
            # cosine distance and pgvector's cosine index cannot place it, so the text as a
            # whole becomes the one word.
            counts = _counts([" ".join(words) or folded.strip()], self.dim)
        return _unit(counts)


class LocalEmbedder:
    """BAAI/bge-small-en-v1.5 through fastembed, on the CPU of this machine.

    The model loads on the first call to embed(): the import of fastembed, and a download of
    the model (66 MB) when it is not in the cache folder yet. fastembed keeps it in
    FASTEMBED_CACHE_PATH, or under the system temp directory when that is unset. Pass
    `cache_dir` to choose the folder in code, for example to keep the model across reboots.

    embed() blocks while the model runs. Async code calls it through asyncio.to_thread.
    Text beyond the 512 tokens that the model reads is cut off.
    """

    dim = EMBEDDING_DIM

    def __init__(self, cache_dir: str | None = None) -> None:
        self._cache_dir = cache_dir
        self._model: TextEmbedding | None = None
        # Two threads that embed for the first time together must not both load the model.
        self._lock = threading.Lock()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []  # nothing to embed is not worth loading a model
        model = self._load()
        # fastembed already scales its output. Doing it here keeps the unit length that the
        # cosine distance relies on from depending on the library.
        return [_unit([float(x) for x in vector]) for vector in model.embed(texts)]

    def _load(self) -> TextEmbedding:
        with self._lock:
            if self._model is None:
                self._model = _new_model(self._cache_dir)
            return self._model


def _new_model(cache_dir: str | None) -> TextEmbedding:
    try:
        from fastembed import TextEmbedding
    except ModuleNotFoundError as exc:
        if exc.name != "fastembed":
            raise  # fastembed is installed, but one of its own imports is not
        raise ModuleNotFoundError(
            "LocalEmbedder needs fastembed, which is an optional extra. "
            "Install it with: uv sync --extra memory",
            name="fastembed",
        ) from exc
    options = {} if cache_dir is None else {"cache_dir": cache_dir}
    return TextEmbedding(LOCAL_MODEL, **options)
