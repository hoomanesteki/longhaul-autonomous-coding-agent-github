"""memory_items: a hash of each note, so the same note is stored once (LH-603)

Revision ID: 0008_memory_content_hash
Revises: 0007_memory_items
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_memory_content_hash"
down_revision: str | None = "0007_memory_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # sha256 of the note with its case and spacing normalized, as hex. The store writes it
    # (longhaul.memory.store.content_hash). The column is nullable because nothing in the
    # product wrote rows before this step, and a row that something else writes has no hash.
    # NULLs differ from each other in a unique index, so such rows never block one another.
    op.add_column("memory_items", sa.Column("content_hash", sa.Text(), nullable=True))
    op.create_index(
        "uq_memory_items_repo_kind_content_hash",
        "memory_items",
        ["repo", "kind", "content_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_memory_items_repo_kind_content_hash", table_name="memory_items")
    op.drop_column("memory_items", "content_hash")
