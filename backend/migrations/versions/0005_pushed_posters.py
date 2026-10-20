"""remember which posters Coverarr pushed

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("server_items") as batch:
        batch.add_column(sa.Column("pushed_ref", sa.String(length=500), nullable=True))
    with op.batch_alter_table("server_collections") as batch:
        batch.add_column(sa.Column("pushed_ref", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("server_collections") as batch:
        batch.drop_column("pushed_ref")
    with op.batch_alter_table("server_items") as batch:
        batch.drop_column("pushed_ref")
