"""The stale prompt of a weekly digest.

After the digest of a week, the bot asks about a few old items of the inbox: archive them, or
keep them? The row of the week keeps which items it asked about and when, so that each week
asks one time, and an item that was asked about lately is not asked about again so soon.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "digests",
        sa.Column(
            "stale_item_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("digests", sa.Column("stale_sent_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("digests", "stale_sent_at")
    op.drop_column("digests", "stale_item_ids")
