"""The memory_items table of plan section 3.5 (LH-601)."""

from typing import Any, get_args

import pytest
from sqlalchemy import CheckConstraint, Index
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from longhaul.contracts import MemoryItem
from longhaul.db.models import MemoryItemRow
from longhaul.memory.embedder import EMBEDDING_DIM

pytestmark = [pytest.mark.unit, pytest.mark.phase6]


def _ddl() -> str:
    return str(CreateTable(MemoryItemRow.__table__).compile(dialect=postgresql.dialect()))


def _check_text(name: str) -> str:
    checks = {
        c.name: str(c.sqltext)
        for c in MemoryItemRow.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    return checks[name]


def _index(name: str) -> Index:
    (found,) = (i for i in MemoryItemRow.__table__.indexes if i.name == name)
    return found


def _literal_values(field: str) -> tuple[Any, ...]:
    return get_args(MemoryItem.model_fields[field].annotation)


def test_columns_match_the_data_model() -> None:
    columns = {c.name: c for c in MemoryItemRow.__table__.columns}
    assert set(columns) == {
        "id", "repo", "kind", "content", "embedding", "status", "source", "source_ref",
        "approved_by", "uses", "last_used_at", "created_at", "content_hash",
    }  # fmt: skip
    assert columns["id"].primary_key
    # The plan marks approved_by "null". A reference exists only for what came from a commit,
    # a pull request or a task, and an item has no use until something retrieves it. The hash
    # is LH-603's: a row that something other than the store wrote has none.
    assert {name for name, column in columns.items() if column.nullable} == {
        "source_ref",
        "approved_by",
        "last_used_at",
        "content_hash",
    }


def test_the_id_is_a_uuid_that_the_database_makes() -> None:
    ddl = _ddl()
    assert "id UUID DEFAULT gen_random_uuid() NOT NULL" in ddl
    assert "CONSTRAINT pk_memory_items PRIMARY KEY (id)" in ddl


def test_a_row_is_a_candidate_until_something_approves_it() -> None:
    # The write policy of ADR-0010 at the lowest level: an insert that says nothing about
    # trust can never put an item in front of the model.
    assert "status TEXT DEFAULT 'candidate' NOT NULL" in _ddl()


def test_the_defaults_are_server_side() -> None:
    ddl = _ddl()
    assert "uses INTEGER DEFAULT 0 NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


def test_the_embedding_is_a_vector_as_wide_as_the_embedders() -> None:
    assert "embedding VECTOR(384) NOT NULL" in _ddl()
    assert MemoryItemRow.__table__.c.embedding.type.dim == EMBEDDING_DIM


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("ck_memory_items_kind", "kind"),
        ("ck_memory_items_status", "status"),
        ("ck_memory_items_source", "source"),
    ],
)
def test_each_check_lists_the_values_of_the_contract(name: str, field: str) -> None:
    # The contract's Literal types and the table must not drift apart, or a store that
    # validates with one would be refused by the other.
    values = _literal_values(field)
    text = _check_text(name)
    for value in values:
        assert f"'{value}'" in text
    assert text.count("'") == 2 * len(values), "only the values of the contract belong in it"


def test_the_only_checks_are_the_three_value_lists() -> None:
    names = {c.name for c in MemoryItemRow.__table__.constraints if isinstance(c, CheckConstraint)}
    assert names == {"ck_memory_items_kind", "ck_memory_items_status", "ck_memory_items_source"}


def test_the_embedding_index_is_hnsw_on_cosine_distance() -> None:
    index = _index("ix_memory_items_embedding")
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert index.name == "ix_memory_items_embedding"
    assert "USING hnsw (embedding vector_cosine_ops)" in ddl
    # pgvector's own defaults, written out so a change of the default does not change the index.
    assert "WITH (m = 16, ef_construction = 64)" in ddl


def test_the_hash_of_a_note_is_unique_within_a_repo_and_kind() -> None:
    # De-duplication (LH-603): the same note cannot be stored twice for one repo and kind.
    index = _index("uq_memory_items_repo_kind_content_hash")
    assert index.unique
    assert [c.name for c in index.columns] == ["repo", "kind", "content_hash"]
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "CREATE UNIQUE INDEX uq_memory_items_repo_kind_content_hash" in ddl


def test_the_table_has_the_two_indexes_and_no_more() -> None:
    names = {i.name for i in MemoryItemRow.__table__.indexes}
    assert names == {"ix_memory_items_embedding", "uq_memory_items_repo_kind_content_hash"}
