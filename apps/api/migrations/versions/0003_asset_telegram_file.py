"""Remember which file of Telegram an asset came from.

The bot keeps the ids of each file that you send. The worker downloads a file when an
extractor needs its bytes, and keeps a copy in the blob store. This column keeps the stable id
of the file of Telegram (``file_unique_id``) with the copy. So the worker finds the copy again,
and does not download the same file a second time. A file of an export has no such id.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("assets", sa.Column("file_unique_id", sa.Text(), nullable=True))
    op.create_index(
        "uq_assets_telegram_file",
        "assets",
        ["owner_id", "file_unique_id"],
        unique=True,
        postgresql_where="file_unique_id IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_assets_telegram_file",
        table_name="assets",
        postgresql_where="file_unique_id IS NOT NULL",
    )
    op.drop_column("assets", "file_unique_id")
