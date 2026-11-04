"""plans: the versions of the plan of each task (LH-501)

Revision ID: 0011_plans
Revises: 0008_memory_content_hash
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011_plans"
# Other stories add 0009 and 0010 on the same parent. The step that merges them re-points this
# one after them, so the history keeps a single head.
down_revision: str | None = "0008_memory_content_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'proposed'"), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'approved', 'rejected', 'superseded')",
            name=op.f("ck_plans_status"),
        ),
        sa.PrimaryKeyConstraint("task_id", "version", name=op.f("pk_plans")),
    )


def downgrade() -> None:
    op.drop_table("plans")
