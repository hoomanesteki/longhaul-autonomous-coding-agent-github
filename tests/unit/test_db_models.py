import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from longhaul.db.models import Base, RepoSetting, Task

pytestmark = [pytest.mark.unit, pytest.mark.phase0]


def test_baseline_tables_registered() -> None:
    assert set(Base.metadata.tables) == {"repo_settings", "tasks"}


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
