"""server item file paths

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("server_items") as batch:
        batch.add_column(sa.Column("path", sa.String(length=1000), nullable=True))
        batch.add_column(sa.Column("file_key", sa.String(length=500), nullable=True))
        batch.create_index("ix_server_items_file_key", ["file_key"])


def downgrade() -> None:
    with op.batch_alter_table("server_items") as batch:
        batch.drop_index("ix_server_items_file_key")
        batch.drop_column("file_key")
        batch.drop_column("path")
