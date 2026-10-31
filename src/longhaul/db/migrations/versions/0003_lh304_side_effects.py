"""side_effects: the idempotency ledger for GitHub writes (LH-304)

Revision ID: 0003_lh304_side_effects
Revises: 0002_checkpoints
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_lh304_side_effects"
down_revision: str | None = "0002_checkpoints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "side_effects",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("payload_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('comment', 'label_add', 'label_remove', 'pr', 'push')",
            name=op.f("ck_side_effects_kind"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_side_effects")),
    )
    op.create_index("ix_side_effects_task_id", "side_effects", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_side_effects_task_id", table_name="side_effects")
    op.drop_table("side_effects")
