"""The side_effects table of plan section 3.5 (LH-304)."""

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from longhaul.db.models import SideEffect

pytestmark = [pytest.mark.unit, pytest.mark.phase3]


def _ddl() -> str:
    return str(CreateTable(SideEffect.__table__).compile(dialect=postgresql.dialect()))


def test_columns_match_the_data_model() -> None:
    columns = {c.name: c for c in SideEffect.__table__.columns}
    assert set(columns) == {"key", "task_id", "kind", "external_id", "payload_hash", "created_at"}
    assert columns["key"].primary_key
    # A ledger row is written after the effect happened, so every column has a value.
    assert not any(column.nullable for column in columns.values())


def test_the_key_is_the_primary_key_and_created_at_has_a_default() -> None:
    ddl = _ddl()
    assert "CONSTRAINT pk_side_effects PRIMARY KEY (key)" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


def test_kind_is_limited_to_the_five_kinds_of_the_plan() -> None:
    checks = [c for c in SideEffect.__table__.constraints if isinstance(c, CheckConstraint)]
    assert [c.name for c in checks] == ["ck_side_effects_kind"]
    text = str(checks[0].sqltext)
    for kind in ("comment", "label_add", "label_remove", "pr", "push"):
        assert f"'{kind}'" in text
    assert text.count("'") == 10, "only the five kinds belong in the check"


def test_rows_are_found_by_task() -> None:
    assert {index.name for index in SideEffect.__table__.indexes} == {"ix_side_effects_task_id"}
