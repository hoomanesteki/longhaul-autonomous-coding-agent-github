import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from longhaul.db.models import Base, RepoSetting, Task, ToolCallRow, Turn

pytestmark = [pytest.mark.unit, pytest.mark.phase0]


def test_baseline_tables_registered() -> None:
    # Each story that adds a table adds it here: LH-205 checkpoints, LH-204 turns and tool_calls.
    tables = {"repo_settings", "tasks", "checkpoints", "turns", "tool_calls"}
    assert set(Base.metadata.tables) == tables


def test_task_columns_match_data_model() -> None:
    columns = {c.name: c for c in Task.__table__.columns}
    assert set(columns) == {
        "id", "repo", "issue_number", "title", "status", "phase", "outcome", "pr_url",
        "model_coder", "cost_usd", "turns", "started_at", "ended_at", "settings", "created_at",
    }  # fmt: skip
    assert columns["id"].primary_key
    assert columns["issue_number"].nullable
    assert not columns["repo"].nullable


def test_task_indexes_and_pk_name() -> None:
    assert {ix.name for ix in Task.__table__.indexes} == {"ix_tasks_repo", "ix_tasks_status"}
    ddl = str(CreateTable(Task.__table__).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT pk_tasks PRIMARY KEY (id)" in ddl
    assert "NUMERIC(10, 4)" in ddl


def test_repo_settings_defaults_are_server_side() -> None:
    ddl = str(CreateTable(RepoSetting.__table__).compile(dialect=postgresql.dialect()))
    assert "allowlisted BOOLEAN DEFAULT false NOT NULL" in ddl
    assert "settings JSONB DEFAULT '{}'::jsonb NOT NULL" in ddl


# ---------- turns and tool_calls (LH-204, plan section 3.5) ----------
def test_turn_columns_match_data_model() -> None:
    columns = {c.name: c for c in Turn.__table__.columns}
    assert set(columns) == {
        "task_id", "turn_index", "attempt", "role", "request_ref", "response_ref",
        "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
        "cost_usd", "stop_reason", "latency_ms", "started_at", "ended_at", "created_at",
    }  # fmt: skip
    keys = [c.name for c in Turn.__table__.primary_key.columns]
    assert keys == ["task_id", "turn_index", "attempt"]


def test_a_turn_row_can_be_a_failed_call_that_was_billed() -> None:
    # A call that failed after the provider billed it has usage and no reply, so everything
    # about the reply may be missing.
    columns = {c.name: c for c in Turn.__table__.columns}
    for name in ("request_ref", "response_ref", "stop_reason", "latency_ms", "ended_at"):
        assert columns[name].nullable, name
    for name in ("role", "started_at", "input_tokens", "output_tokens", "cost_usd"):
        assert not columns[name].nullable, name


def test_turn_ddl() -> None:
    ddl = str(CreateTable(Turn.__table__).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT pk_turns PRIMARY KEY (task_id, turn_index, attempt)" in ddl
    assert "input_tokens INTEGER DEFAULT 0 NOT NULL" in ddl
    assert "cache_write_tokens INTEGER DEFAULT 0 NOT NULL" in ddl
    assert "cost_usd NUMERIC(10, 6) DEFAULT 0 NOT NULL" in ddl
    assert "started_at TIMESTAMP WITH TIME ZONE NOT NULL" in ddl
    assert "created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


def test_tool_call_columns_match_data_model() -> None:
    columns = {c.name: c for c in ToolCallRow.__table__.columns}
    assert set(columns) == {
        "id", "task_id", "turn_index", "name", "args_ref", "ok", "exit_code", "output_ref",
        "duration_ms", "pre_sha", "post_sha", "attempt", "created_at",
    }  # fmt: skip
    assert [c.name for c in ToolCallRow.__table__.primary_key.columns] == ["id"]
    for name in ("args_ref", "exit_code", "output_ref", "pre_sha", "post_sha"):
        assert columns[name].nullable, name
    for name in ("task_id", "turn_index", "name", "ok", "duration_ms", "attempt"):
        assert not columns[name].nullable, name


def test_tool_call_ddl_and_index() -> None:
    ddl = str(CreateTable(ToolCallRow.__table__).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT pk_tool_calls PRIMARY KEY (id)" in ddl
    assert "duration_ms INTEGER DEFAULT 0 NOT NULL" in ddl
    assert "attempt INTEGER DEFAULT 1 NOT NULL" in ddl
    # The console lists the calls of a task in turn order.
    assert {ix.name for ix in ToolCallRow.__table__.indexes} == {"ix_tool_calls_task_turn"}
