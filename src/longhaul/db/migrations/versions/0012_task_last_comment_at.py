"""tasks: the newest issue comment handled for each task (LH-308)

Revision ID: 0012_task_last_comment_at
Revises: 0011_plans
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_task_last_comment_at"
down_revision: str | None = "0011_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Both columns are null until the poller has picked the task up with a cursor. A row that
    # was written before this migration has none, and the poller then reads its comments from
    # the time the task started. GitHub stamps a comment to the second, so the time alone cannot
    # tell two comments of one second apart. The id, which only grows, can.
    op.add_column("tasks", sa.Column("last_comment_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("last_comment_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "last_comment_id")
    op.drop_column("tasks", "last_comment_at")
