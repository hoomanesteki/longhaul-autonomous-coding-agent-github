"""cost_ledger: one row for every model call (LH-701)

Revision ID: 0006_cost_ledger
Revises: 0005_events
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_cost_ledger"
down_revision: str | None = "0005_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cost_ledger",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cache_write_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cost_ledger")),
    )
    op.create_index("ix_cost_ledger_task_id", "cost_ledger", ["task_id"])
    op.create_index("ix_cost_ledger_repo_ts", "cost_ledger", ["repo", "ts"])


def downgrade() -> None:
    op.drop_index("ix_cost_ledger_repo_ts", table_name="cost_ledger")
    op.drop_index("ix_cost_ledger_task_id", table_name="cost_ledger")
    op.drop_table("cost_ledger")
