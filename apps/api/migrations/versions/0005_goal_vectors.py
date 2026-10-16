"""The vectors of the goals, for the prefilter of the scores.

The score step compares the vector of the card of an item with the vector of each goal. When
the card is far from each goal, the goals get 0 without a question to the model. A goal vector
belongs to one goal and one embedding set, and it keeps the version of the goal text that it
holds: a goal with a new meaning gets a new vector.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector  # pyright: ignore[reportMissingTypeStubs]

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "goal_vectors",
        sa.Column("goal_id", sa.BigInteger(), nullable=False),
        sa.Column("embedding_set_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_id", sa.BigInteger(), nullable=False),
        sa.Column("goal_version", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "vector_dims(embedding) = dim", name=op.f("ck_goal_vectors_dim_matches_vector")
        ),
        sa.ForeignKeyConstraint(
            ["embedding_set_id"],
            ["embedding_sets.id"],
            name=op.f("fk_goal_vectors_embedding_set_id_embedding_sets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["goal_id"],
            ["goals.id"],
            name=op.f("fk_goal_vectors_goal_id_goals"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_goal_vectors_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("goal_id", "embedding_set_id", name=op.f("pk_goal_vectors")),
    )


def downgrade() -> None:
    op.drop_table("goal_vectors")
