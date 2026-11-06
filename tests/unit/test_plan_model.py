"""The plans table of plan section 3.5 (LH-501)."""

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from longhaul.db.models import PlanRow

pytestmark = [pytest.mark.unit, pytest.mark.phase5]


def _ddl() -> str:
    return str(CreateTable(PlanRow.__table__).compile(dialect=postgresql.dialect()))


def test_columns_match_the_data_model() -> None:
    columns = {c.name: c for c in PlanRow.__table__.columns}
    assert set(columns) == {
        "task_id", "version", "plan", "status", "decided_by", "decided_at", "created_at",
    }  # fmt: skip
    # Only the decision is optional: a plan is proposed first and decided on later.
    assert {name for name, c in columns.items() if c.nullable} == {"decided_by", "decided_at"}


def test_a_task_has_one_row_for_each_version_of_its_plan() -> None:
    keys = [c.name for c in PlanRow.__table__.primary_key.columns]
    assert keys == ["task_id", "version"]
    assert "CONSTRAINT pk_plans PRIMARY KEY (task_id, version)" in _ddl()


def test_the_plan_is_json_and_a_new_row_is_proposed() -> None:
    ddl = _ddl()
    assert "plan JSONB NOT NULL" in ddl
    assert "status TEXT DEFAULT 'proposed' NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


def test_status_is_limited_to_the_four_states_of_the_plan() -> None:
    checks = [c for c in PlanRow.__table__.constraints if isinstance(c, CheckConstraint)]
    assert [c.name for c in checks] == ["ck_plans_status"]
    text = str(checks[0].sqltext)
    for state in ("proposed", "approved", "rejected", "superseded"):
        assert f"'{state}'" in text
    assert text.count("'") == 8, "only the four states belong in the check"


def test_rows_are_found_by_task_through_the_primary_key() -> None:
    # task_id leads the primary key, so a lookup by task needs no index of its own.
    assert {index.name for index in PlanRow.__table__.indexes} == set()
