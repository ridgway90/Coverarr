"""media facts for overlays, and what was pushed

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20 19:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_items") as batch:
        batch.add_column(sa.Column("facts", sa.JSON(), nullable=True))
    with op.batch_alter_table("server_items") as batch:
        batch.add_column(sa.Column("media", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("pushed_key", sa.String(length=100), nullable=True))
    with op.batch_alter_table("server_collections") as batch:
        batch.add_column(sa.Column("pushed_key", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("server_collections") as batch:
        batch.drop_column("pushed_key")
    with op.batch_alter_table("server_items") as batch:
        batch.drop_column("pushed_key")
        batch.drop_column("media")
    with op.batch_alter_table("media_items") as batch:
        batch.drop_column("facts")
