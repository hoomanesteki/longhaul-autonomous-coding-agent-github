"""The embedders of the memory store (LH-601, ADR-0010).

HashEmbedder has to give the same vector in every process and on every run, because a vector
stored today is compared with a query embedded next week. LocalEmbedder is checked here
against a stand-in for fastembed, so these tests download no model. The real model runs in
tests/slow/test_local_embedder.py.
"""

import json
import math
import os
import subprocess
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from longhaul.db.models import MemoryItemRow
from longhaul.memory.embedder import EMBEDDING_DIM, LOCAL_MODEL, HashEmbedder, LocalEmbedder

pytestmark = [pytest.mark.unit, pytest.mark.phase6]

SRC = Path(__file__).resolve().parents[2] / "src"
TEXTS = [
    "Raise InventoryError, not ValueError, when an item is missing.",
    "Run pytest -q before you call finish.",
    "",
]


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def _cosine(a: list[float], b: list[float]) -> float:
    # Both vectors have unit length, so the dot product is the cosine.
    return sum(x * y for x, y in zip(a, b, strict=True))


def _run_child(code: str, *args: str, hash_seed: str = "0") -> str:
    """Run `code` in a fresh interpreter and return its output."""
    # A child interpreter does not get pytest's pythonpath, and on macOS Python skips an
    # editable-install .pth file that carries the hidden flag, so name src explicitly.
    env = {
        **os.environ,
        "PYTHONHASHSEED": hash_seed,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC), os.environ.get("PYTHONPATH")])),
    }
    done = subprocess.run(
        [sys.executable, "-c", code, *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


EMBED_IN_CHILD = """
import json, sys
from longhaul.memory.embedder import HashEmbedder
print(json.dumps(HashEmbedder().embed(json.loads(sys.argv[1]))))
"""


# ---------- HashEmbedder ----------
def test_hash_embedder_deterministic() -> None:
    first = HashEmbedder().embed(TEXTS)

    assert HashEmbedder().embed(TEXTS) == first
    # Python's hash() of a str changes with PYTHONHASHSEED, so an embedder built on it would
    # give a worker restart different vectors from the ones already stored. A child
    # interpreter with another seed catches that. JSON keeps every float exactly.
    for seed in ("1", "2718"):
        child = json.loads(_run_child(EMBED_IN_CHILD, json.dumps(TEXTS), hash_seed=seed))
        assert child == first, f"PYTHONHASHSEED={seed} changed the vectors"


def test_the_scheme_has_not_changed_since_rows_were_stored() -> None:
    # Rows already in the table were embedded by an earlier run, so a change to the tokens,
    # the hash or the signs would leave every old vector in a different place than a new
    # query. If this test fails on purpose, the stored vectors need to be embedded again.
    vector = HashEmbedder().embed(["remove the item"])[0]

    # Three words in three buckets, each with weight 1 / sqrt(3). The buckets and signs are
    # blake2b(word, digest_size=8, key=b"longhaul-hash-embedder-v1"): the first four bytes
    # modulo 384 and the low bit of the fifth.
    assert {i: round(x, 6) for i, x in enumerate(vector) if x} == {
        179: -0.57735,  # remove
        310: 0.57735,  # item
        362: -0.57735,  # the
    }


@pytest.mark.parametrize("dim", [EMBEDDING_DIM, 64, 1])
def test_vectors_have_the_asked_width_and_unit_length(dim: int) -> None:
    embedder = HashEmbedder(dim=dim)

    vectors = embedder.embed(TEXTS)

    assert embedder.dim == dim
    assert [len(v) for v in vectors] == [dim] * len(TEXTS)
    assert [_norm(v) for v in vectors] == pytest.approx([1.0] * len(TEXTS))


def test_the_default_width_is_the_width_of_the_table_column() -> None:
    column = MemoryItemRow.__table__.c.embedding
    assert HashEmbedder().dim == EMBEDDING_DIM == column.type.dim == 384


def test_texts_that_share_words_are_closer_than_texts_that_do_not() -> None:
    lesson, other, query = HashEmbedder().embed(
        [
            "Raise InventoryError when an item is missing from the inventory.",
            "Format the code with ruff and keep lines under 100 characters.",
            "Which error does remove raise for a missing item in the inventory?",
        ]
    )

    assert _cosine(query, lesson) > _cosine(query, other)
    assert _cosine(query, query) == pytest.approx(1.0)


def test_case_punctuation_and_word_order_do_not_change_the_vector() -> None:
    # Full-width letters (U+FF41 and up) fold to the plain ones under NFKC, as in text pasted
    # from a PDF. They sit 0xFEE0 above their ASCII twins.
    fullwidth = "".join(chr(ord(letter) + 0xFEE0) for letter in "remove")
    plain, shouted, reordered, wide = HashEmbedder().embed(
        [
            "remove the item",
            "REMOVE, the item!",
            "item... the remove",
            f"{fullwidth} the item",
        ]
    )

    assert shouted == plain
    assert reordered == plain
    assert wide == plain


def test_an_underscore_separates_words_like_a_space() -> None:
    # Code is full of snake_case names, and a lesson that says remove_item should match a
    # query that says "remove item".
    snake, spaced = HashEmbedder().embed(["remove_item", "remove item"])
    assert snake == spaced


@pytest.mark.parametrize("text", ["", "   ", "!!! ???", "\n"])
def test_a_text_without_words_still_gets_a_unit_vector(text: str) -> None:
    # A zero vector has no cosine distance, and pgvector's cosine index cannot place it.
    (vector,) = HashEmbedder().embed([text])
    assert _norm(vector) == pytest.approx(1.0)


def test_words_that_cancel_each_other_still_give_a_unit_vector() -> None:
    # With one bucket every word lands in the same place, and two words with opposite signs
    # add up to zero. The embedder then hashes the whole text as one word.
    embedder = HashEmbedder(dim=1)
    signs = {word: embedder.embed([word])[0][0] for word in (f"word{n}" for n in range(16))}
    positive = next(word for word, sign in signs.items() if sign > 0)
    negative = next(word for word, sign in signs.items() if sign < 0)

    (vector,) = embedder.embed([f"{positive} {negative}"])

    assert _norm(vector) == pytest.approx(1.0)


def test_a_batch_gives_the_same_vectors_as_single_texts() -> None:
    embedder = HashEmbedder()
    assert embedder.embed(TEXTS) == [embedder.embed([text])[0] for text in TEXTS]
    assert embedder.embed([]) == []


@pytest.mark.parametrize("dim", [0, -3])
def test_the_width_must_be_positive(dim: int) -> None:
    with pytest.raises(ValueError, match="dim"):
        HashEmbedder(dim=dim)


# ---------- LocalEmbedder ----------
class FakeFastembed:
    """Stands in for the fastembed module.

    Vector i of a batch holds `hot` at position i and zeros elsewhere. The default of 5.0 is
    not unit length on purpose: LocalEmbedder must not rely on the library to scale it.
    """

    def __init__(self, hot: float = 5.0) -> None:
        self.loaded: list[dict[str, Any]] = []
        self.batches: list[list[str]] = []
        outer = self

        class TextEmbedding:
            def __init__(self, model_name: str, **options: Any) -> None:
                outer.loaded.append({"model_name": model_name, **options})

            def embed(self, documents: list[str]) -> Iterator[list[float]]:
                outer.batches.append(list(documents))
                for index, _ in enumerate(documents):
                    yield [hot if i == index else 0.0 for i in range(EMBEDDING_DIM)]

        self.module = types.ModuleType("fastembed")
        self.module.TextEmbedding = TextEmbedding


@pytest.fixture
def fastembed(monkeypatch: pytest.MonkeyPatch) -> FakeFastembed:
    fake = FakeFastembed()
    monkeypatch.setitem(sys.modules, "fastembed", fake.module)
    return fake


def test_local_embedder_loads_the_model_on_first_use_only(fastembed: FakeFastembed) -> None:
    embedder = LocalEmbedder()
    assert fastembed.loaded == []

    embedder.embed(["one"])
    embedder.embed(["two", "three"])

    assert fastembed.loaded == [{"model_name": LOCAL_MODEL}]
    assert fastembed.batches == [["one"], ["two", "three"]]


def test_local_embedder_names_the_model_of_the_adr() -> None:
    assert LOCAL_MODEL == "BAAI/bge-small-en-v1.5"
    assert LocalEmbedder().dim == EMBEDDING_DIM


def test_local_embedder_passes_the_cache_folder_on(fastembed: FakeFastembed) -> None:
    LocalEmbedder(cache_dir="/var/cache/longhaul").embed(["one"])
    assert fastembed.loaded == [{"model_name": LOCAL_MODEL, "cache_dir": "/var/cache/longhaul"}]


def test_local_embedder_returns_unit_vectors_in_the_order_of_the_texts(
    fastembed: FakeFastembed,
) -> None:
    vectors = LocalEmbedder().embed(["a", "b", "c"])

    assert [len(v) for v in vectors] == [EMBEDDING_DIM] * 3
    assert [_norm(v) for v in vectors] == pytest.approx([1.0, 1.0, 1.0])
    assert [v.index(1.0) for v in vectors] == [0, 1, 2]


def test_local_embedder_does_not_load_a_model_for_an_empty_batch(fastembed: FakeFastembed) -> None:
    assert LocalEmbedder().embed([]) == []
    assert fastembed.loaded == []


def test_local_embedder_refuses_a_vector_of_length_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    # No cosine distance can rank a zero vector, so it must not reach the table.
    monkeypatch.setitem(sys.modules, "fastembed", FakeFastembed(hot=0.0).module)

    with pytest.raises(ValueError, match="length zero"):
        LocalEmbedder().embed(["one"])


def test_local_embedder_says_how_to_install_a_missing_fastembed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "fastembed", None)  # makes `import fastembed` fail

    with pytest.raises(ImportError, match=r"uv sync --extra memory"):
        LocalEmbedder().embed(["one"])


def test_a_missing_dependency_of_fastembed_is_not_reported_as_a_missing_fastembed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With fastembed installed but onnxruntime broken, "install the memory extra" would send
    # the reader the wrong way. The original error says what is missing.
    broken = types.ModuleType("fastembed")

    def missing(name: str) -> Any:
        raise ModuleNotFoundError("No module named 'onnxruntime'", name="onnxruntime")

    broken.__dict__["__getattr__"] = missing  # a module-level __getattr__ (PEP 562)
    monkeypatch.setitem(sys.modules, "fastembed", broken)

    with pytest.raises(ModuleNotFoundError, match="onnxruntime") as raised:
        LocalEmbedder().embed(["one"])
    assert raised.value.name == "onnxruntime"


def test_importing_the_module_does_not_import_fastembed() -> None:
    # fastembed brings in onnxruntime and numpy, which a worker that only stores and reads
    # memory should not pay for. This also holds on a machine that has the extra installed.
    code = "import sys\nimport longhaul.memory.embedder\nprint('fastembed' in sys.modules)\n"
    assert _run_child(code).strip() == "False"


# The signature of fastembed 0.9.1, which ships py.typed. With the memory extra installed,
# mypy checks the call in LocalEmbedder against it. Without the extra there is nothing to
# check, which is how CI and the base install stayed green while `make check` failed for a
# developer who had run `uv sync --extra memory`.
FASTEMBED_STUB = """\
from collections.abc import Iterable, Iterator, Sequence
from enum import Enum
from typing import Any

class Device(str, Enum):
    CPU = "cpu"
    CUDA = "cuda"
    AUTO = "auto"

class TextEmbedding:
    def __init__(
        self,
        model_name: str = ...,
        cache_dir: str | None = None,
        threads: int | None = None,
        providers: Sequence[Any] | None = None,
        cuda: bool | Device = ...,
        device_ids: list[int] | None = None,
        lazy_load: bool = False,
        **kwargs: Any,
    ) -> None: ...
    def embed(self, documents: str | Iterable[str], batch_size: int = 256) -> Iterator[Any]: ...
"""


def test_the_model_loader_type_checks_against_the_signature_of_fastembed(tmp_path: Path) -> None:
    stubs = tmp_path / "stubs" / "fastembed"
    stubs.mkdir(parents=True)
    (stubs / "__init__.pyi").write_text(FASTEMBED_STUB, encoding="utf-8")
    root = SRC.parent
    env = {**os.environ, "MYPYPATH": str(stubs.parent)}

    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--cache-dir",
            str(tmp_path / ".cache"),
            "-m",
            "longhaul.memory.embedder",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert done.returncode == 0, done.stdout
