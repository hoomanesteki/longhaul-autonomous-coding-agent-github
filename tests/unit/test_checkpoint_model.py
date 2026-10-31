"""The checkpoints table of plan section 3.5 (LH-205)."""

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from longhaul.db.models import Checkpoint

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


def _ddl() -> str:
    return str(CreateTable(Checkpoint.__table__).compile(dialect=postgresql.dialect()))


def test_columns_match_the_data_model() -> None:
    columns = {c.name: c for c in Checkpoint.__table__.columns}
    assert set(columns) == {
        "id", "task_id", "sha", "kind", "step_id", "turn_index", "bundle_ref", "created_at",
    }  # fmt: skip
    assert columns["id"].primary_key
    # The plan marks step_id and bundle_ref "null". Every other column is required.
    assert {name for name, column in columns.items() if column.nullable} == {
        "step_id",
        "bundle_ref",
    }


def test_id_is_a_serial_and_the_ref_is_json() -> None:
    ddl = _ddl()
    assert "id SERIAL NOT NULL" in ddl
    assert "CONSTRAINT pk_checkpoints PRIMARY KEY (id)" in ddl
    # A BlobRef is key, sha256 and size. Keeping all three lets a reader verify the bundle.
    assert "bundle_ref JSONB" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


def test_kind_is_limited_to_the_four_kinds_of_the_plan() -> None:
    checks = [c for c in Checkpoint.__table__.constraints if isinstance(c, CheckConstraint)]
    assert [c.name for c in checks] == ["ck_checkpoints_kind"]
    text = str(checks[0].sqltext)
    for kind in ("tool", "step", "compaction", "bundle"):
        assert f"'{kind}'" in text
    assert text.count("'") == 8, "only the four kinds belong in the check"


def test_rows_are_found_by_task() -> None:
    assert {index.name for index in Checkpoint.__table__.indexes} == {"ix_checkpoints_task_id"}
