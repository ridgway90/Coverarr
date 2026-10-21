"""collection definitions that Coverarr keeps in sync on every server

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-21 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("collection_definitions") as batch:
        batch.add_column(
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("sync_result", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("collection_definitions") as batch:
        batch.drop_column("synced_at")
        batch.drop_column("sync_result")
        batch.drop_column("enabled")
