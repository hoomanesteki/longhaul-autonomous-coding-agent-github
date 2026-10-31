"""checkpoints: one row per saved workspace state (LH-205)

Revision ID: 0002_checkpoints
Revises: 0001_baseline
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_checkpoints"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "checkpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("sha", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("step_id", sa.Text(), nullable=True),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("bundle_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('tool', 'step', 'compaction', 'bundle')",
            name=op.f("ck_checkpoints_kind"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_checkpoints")),
    )
    op.create_index("ix_checkpoints_task_id", "checkpoints", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_checkpoints_task_id", table_name="checkpoints")
    op.drop_table("checkpoints")
