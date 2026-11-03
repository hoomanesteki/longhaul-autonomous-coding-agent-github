"""LocalEmbedder against the real model (LH-601, ADR-0010). It is opt-in, because the model is a
download.

    uv sync --extra memory
    uv run pytest -m slow tests/slow/test_local_embedder.py -rs

Without fastembed the tests are skipped and the reason says how to install it. CI does not
install the extra, and gates, `make itest` and `make e2e` leave slow tests out. The first run
downloads the model into fastembed's cache folder, which is FASTEMBED_CACHE_PATH when that is
set and a folder under the system temp directory otherwise.
"""

import math

import pytest

from longhaul.memory.embedder import EMBEDDING_DIM, LocalEmbedder

pytestmark = [pytest.mark.slow, pytest.mark.phase6]


@pytest.fixture(scope="module")
def embedder() -> LocalEmbedder:
    pytest.importorskip(
        "fastembed",
        reason="LH-601: the memory extra is not installed; run `uv sync --extra memory` first",
    )
    return LocalEmbedder()


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_dim(embedder: LocalEmbedder) -> None:
    vectors = embedder.embed(["Raise InventoryError, not ValueError.", "Run the tests.", ""])

    assert embedder.dim == EMBEDDING_DIM == 384
    assert [len(vector) for vector in vectors] == [384, 384, 384]
    for vector in vectors:
        assert math.sqrt(sum(x * x for x in vector)) == pytest.approx(1.0, abs=1e-5)


def test_the_same_text_gives_the_same_vector(embedder: LocalEmbedder) -> None:
    text = "Raise InventoryError, not ValueError."
    first, second = embedder.embed([text]), embedder.embed([text])
    assert second[0] == pytest.approx(first[0], abs=1e-6)


def test_texts_with_the_same_meaning_are_closer_than_unrelated_ones(
    embedder: LocalEmbedder,
) -> None:
    # The point of the real model over HashEmbedder: these share no word.
    query, same_meaning, unrelated = embedder.embed(
        [
            "fix the login bug",
            "repair the sign-in defect",
            "banana bread recipe with walnuts",
        ]
    )

    assert _cosine(query, same_meaning) > _cosine(query, unrelated)
