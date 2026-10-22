"""details fetched from TMDb (and MDBList): streaming services, ratings, status, people

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-22 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_items") as batch:
        batch.add_column(sa.Column("details", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("media_items") as batch:
        batch.drop_column("details")
