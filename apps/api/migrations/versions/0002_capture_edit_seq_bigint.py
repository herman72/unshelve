"""Make the edit number of a capture a 64-bit number.

The edit number of a capture is the time of the edit, in seconds since 1970. The bot gets
that number from Telegram, and the same edit gives the same number each time it arrives. A
32-bit column cannot hold such a number after January 2038.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "captures",
        "edit_seq",
        type_=sa.BigInteger(),
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_server_default="0",
    )


def downgrade() -> None:
    op.alter_column(
        "captures",
        "edit_seq",
        type_=sa.Integer(),
        existing_type=sa.BigInteger(),
        existing_nullable=False,
        existing_server_default="0",
    )
