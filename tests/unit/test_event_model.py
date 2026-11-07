"""The events table of plan section 3.5 (LH-206)."""

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from longhaul.db.models import Event

pytestmark = [pytest.mark.unit, pytest.mark.phase2]


def _ddl() -> str:
    return str(CreateTable(Event.__table__).compile(dialect=postgresql.dialect()))


def test_columns_match_the_data_model() -> None:
    columns = {c.name: c for c in Event.__table__.columns}
    assert set(columns) == {"id", "task_id", "ts", "type", "payload", "created_at"}
    assert columns["id"].primary_key
    # An event always says which task it belongs to, when it happened, what kind it is and
    # what it carried, so nothing is optional.
    assert not any(column.nullable for column in columns.values())


def test_id_is_a_serial_and_both_times_default_to_now() -> None:
    ddl = _ddl()
    assert "id SERIAL NOT NULL" in ddl
    assert "CONSTRAINT pk_events PRIMARY KEY (id)" in ddl
    assert "ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


def test_the_payload_is_json_and_defaults_to_an_empty_object() -> None:
    assert "payload JSONB DEFAULT '{}'::jsonb NOT NULL" in _ddl()


def test_the_type_is_free_text() -> None:
    # The plan lists the first types, and later stories add more (memory_promoted and
    # memory_rejected in LH-606). A CHECK would make each new type a migration.
    checks = [c for c in Event.__table__.constraints if isinstance(c, CheckConstraint)]
    assert checks == []


def test_rows_are_found_by_task() -> None:
    assert {index.name for index in Event.__table__.indexes} == {"ix_events_task_id"}
