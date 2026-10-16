"""Batches of questions to the model, at half the price.

In batch mode (``UNSHELVE_CARDS=batch``) the card of an item waits for a batch. A command sends
the waiting questions as one batch, and the worker collects the answers when the batch has
ended. ``llm_batches`` keeps each batch, and ``llm_batch_items`` the items whose question is in
it. A row of the ledger names the batch that answered it: a batch that you accepted by hand
does not use the daily budget. The new job ``collect_batch`` reads the answers.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KINDS = (
    "'ingest_capture', 'ingest_album', 'fetch_item', 'enrich_item', 'embed_item', "
    "'score_item', 'send_digest', 'stale_sweep', 'publish_feed'"
)


def upgrade() -> None:
    op.create_table(
        "llm_batches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("provider_batch_id", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="sending", nullable=False),
        sa.Column("questions", sa.Integer(), nullable=False),
        sa.Column("answered", sa.Integer(), server_default="0", nullable=False),
        sa.Column("not_answered", sa.Integer(), server_default="0", nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(precision=12, scale=6), nullable=False),
        sa.Column(
            "cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('sending', 'sent', 'collected', 'failed')",
            name=op.f("ck_llm_batches_state_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_batches")),
        sa.UniqueConstraint("provider_batch_id", name=op.f("uq_llm_batches_provider_batch_id")),
    )
    op.create_table(
        "llm_batch_items",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("custom_id", sa.Text(), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["llm_batches.id"],
            name=op.f("fk_llm_batch_items_batch_id_llm_batches"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["items.id"],
            name=op.f("fk_llm_batch_items_item_id_items"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("batch_id", "item_id", name=op.f("pk_llm_batch_items")),
    )
    op.create_index("ix_llm_batch_items_custom_id", "llm_batch_items", ["custom_id"], unique=False)
    op.add_column("llm_calls", sa.Column("batch_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_llm_calls_batch_id_llm_batches"),
        "llm_calls",
        "llm_batches",
        ["batch_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.create_check_constraint(
        op.f("ck_jobs_kind_valid"), "jobs", f"kind IN ({_KINDS}, 'collect_batch')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'collect_batch'")
    op.drop_constraint(op.f("ck_jobs_kind_valid"), "jobs", type_="check")
    op.create_check_constraint(op.f("ck_jobs_kind_valid"), "jobs", f"kind IN ({_KINDS})")
    op.drop_constraint(op.f("fk_llm_calls_batch_id_llm_batches"), "llm_calls", type_="foreignkey")
    op.drop_column("llm_calls", "batch_id")
    op.drop_index("ix_llm_batch_items_custom_id", table_name="llm_batch_items")
    op.drop_table("llm_batch_items")
    op.drop_table("llm_batches")
