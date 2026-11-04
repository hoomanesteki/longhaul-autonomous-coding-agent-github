"""tasks: token totals, compactions and the largest share of the window (LH-406)

Revision ID: 0010_task_context_totals
Revises: 0008_memory_content_hash
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_task_context_totals"
down_revision: str | None = "0008_memory_content_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The columns that count tokens. A task re-sends its prompt on every turn, so the sum over a long
# task on a big window can pass what 32 bits hold.
_TOKEN_COLUMNS = ("input_tokens_total", "output_tokens_total", "cache_read_total")


def upgrade() -> None:
    # Rows that exist already get 0: nothing wrote these numbers for them, and the cost ledger
    # rows of an old task are not summed back in, since a task that has ended needs no report.
    for name in _TOKEN_COLUMNS:
        op.add_column(
            "tasks",
            sa.Column(name, sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        )
    op.add_column(
        "tasks", sa.Column("compactions", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    # A share of the window, between 0 and 1 (a request over the hard limit is not sent).
    op.add_column(
        "tasks", sa.Column("max_fraction", sa.Float(), server_default=sa.text("0"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column("tasks", "max_fraction")
    op.drop_column("tasks", "compactions")
    for name in reversed(_TOKEN_COLUMNS):
        op.drop_column("tasks", name)
