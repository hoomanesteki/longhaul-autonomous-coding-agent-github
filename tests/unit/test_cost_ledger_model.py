"""The cost_ledger table of plan section 3.5 (LH-701)."""

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from longhaul.db.models import CostLedgerRow

pytestmark = [pytest.mark.unit, pytest.mark.phase7]


def _ddl() -> str:
    return str(CreateTable(CostLedgerRow.__table__).compile(dialect=postgresql.dialect()))


def test_columns_match_the_data_model() -> None:
    columns = {c.name: c for c in CostLedgerRow.__table__.columns}
    assert set(columns) == {
        "id", "task_id", "repo", "model", "turn_index", "attempt", "input_tokens",
        "output_tokens", "cache_read_tokens", "cache_write_tokens", "usd", "ts", "created_at",
    }  # fmt: skip
    assert [c.name for c in CostLedgerRow.__table__.primary_key.columns] == ["id"]
    # A row is written after the call, when every number is known.
    assert not any(column.nullable for column in columns.values())


def test_id_is_a_serial_so_a_turn_can_have_many_rows() -> None:
    # Retries and the second call of a cut or paused turn share a task, turn and attempt, so
    # none of those can be the key.
    ddl = _ddl()
    assert "id SERIAL NOT NULL" in ddl
    assert "CONSTRAINT pk_cost_ledger PRIMARY KEY (id)" in ddl


def test_the_money_and_the_time() -> None:
    ddl = _ddl()
    # Millionths of a dollar, as plan section 3.5 says.
    assert "usd NUMERIC(10, 6) NOT NULL" in ddl
    assert "ts TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


def test_the_token_counts_default_to_zero() -> None:
    ddl = _ddl()
    for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
        assert f"{name} INTEGER DEFAULT 0 NOT NULL" in ddl


def test_rows_are_summed_by_task_and_by_repo_and_day() -> None:
    indexes = {
        index.name: [c.name for c in index.columns] for index in CostLedgerRow.__table__.indexes
    }
    assert indexes == {
        "ix_cost_ledger_task_id": ["task_id"],
        "ix_cost_ledger_repo_ts": ["repo", "ts"],
    }
