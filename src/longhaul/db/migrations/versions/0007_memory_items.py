"""memory_items: what the agent remembers about a repo, with vectors (LH-601)

Revision ID: 0007_memory_items
Revises: 0006_cost_ledger
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision: str = "0007_memory_items"
down_revision: str | None = "0006_cost_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The vector extension comes from 0001_baseline.
    op.create_table(
        "memory_items",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        # 384 is the width of BAAI/bge-small-en-v1.5 (ADR-0010). It is written out here and
        # not read from the code, because a migration must not change when the code does.
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'candidate'"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("uses", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('repo_map', 'command', 'convention', 'pitfall', 'lesson')",
            name=op.f("ck_memory_items_kind"),
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'approved', 'rejected')",
            name=op.f("ck_memory_items_status"),
        ),
        sa.CheckConstraint(
            "source IN ('default_branch', 'merged_pr', 'human', 'task')",
            name=op.f("ck_memory_items_source"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_items")),
    )
    # HNSW needs no rows to build from, unlike ivfflat, and pgvector 0.8.0 (compose and CI)
    # has it. vector_cosine_ops matches the `<=>` operator the retrieval query orders by.
    op.create_index(
        "ix_memory_items_embedding",
        "memory_items",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_embedding", table_name="memory_items")
    op.drop_table("memory_items")
    # The vector extension stays, as 0001_baseline explains.
