"""turns: cache_ratio, the share of a prompt that came from the cache (LH-405)

Revision ID: 0009_turn_cache_ratio
Revises: 0008_memory_content_hash
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_turn_cache_ratio"
down_revision: str | None = "0008_memory_content_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # cache_read / (input + cache_read), the same sum as longhaul.context.tokens.cache_ratio.
    # The default of 0 is the ratio of a call that had no prompt to read from.
    op.add_column(
        "turns",
        sa.Column("cache_ratio", sa.Float(), server_default=sa.text("0"), nullable=False),
    )
    # Rows written before this step have the counts, so the ratio can be worked out for them.
    op.execute(
        "UPDATE turns "
        "SET cache_ratio = cache_read_tokens::float8 / (input_tokens + cache_read_tokens) "
        "WHERE input_tokens + cache_read_tokens > 0"
    )


def downgrade() -> None:
    op.drop_column("turns", "cache_ratio")
