"""backgrounds (fanart) as managed artwork

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-22 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_items") as batch:
        batch.add_column(sa.Column("background_source", sa.String(100), nullable=True))
    with op.batch_alter_table("server_items") as batch:
        batch.add_column(sa.Column("background_ref", sa.String(500), nullable=True))
        batch.add_column(sa.Column("background_pushed_ref", sa.String(500), nullable=True))
        batch.add_column(sa.Column("background_pushed_key", sa.String(100), nullable=True))
    op.create_table(
        "backgrounds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("file_path", sa.String(1000), nullable=False),
        sa.Column("original_name", sa.String(500), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_backgrounds_media", "backgrounds", ["media_item_id"])


def downgrade() -> None:
    op.drop_index("ix_backgrounds_media", table_name="backgrounds")
    op.drop_table("backgrounds")
    with op.batch_alter_table("server_items") as batch:
        batch.drop_column("background_pushed_key")
        batch.drop_column("background_pushed_ref")
        batch.drop_column("background_ref")
    with op.batch_alter_table("media_items") as batch:
        batch.drop_column("background_source")
