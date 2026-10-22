"""metadata edits (title, sort title, summary, labels) pushed to every server

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-22 11:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_items") as batch:
        batch.add_column(sa.Column("metadata_edits", sa.JSON(), nullable=True))
    with op.batch_alter_table("server_items") as batch:
        batch.add_column(sa.Column("metadata_pushed_key", sa.String(64), nullable=True))
        batch.add_column(sa.Column("metadata_pushed_labels", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("server_items") as batch:
        batch.drop_column("metadata_pushed_labels")
        batch.drop_column("metadata_pushed_key")
    with op.batch_alter_table("media_items") as batch:
        batch.drop_column("metadata_edits")
