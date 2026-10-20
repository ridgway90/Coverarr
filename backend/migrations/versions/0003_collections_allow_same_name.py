"""allow collections whose names normalise the same

Two different collections on one server ("Jay and Silent Bob Collection" and the
"Jay & Silent Bob" universe) normalise to the same name, so the unique constraint on
(library_id, normalized_name) is replaced with a plain index.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 15:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 0001 created the constraint unnamed; this convention lets batch mode find it to drop.
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    with op.batch_alter_table("collections", naming_convention=NAMING, recreate="always") as batch:
        batch.drop_constraint("uq_collections_library_id", type_="unique")
        batch.create_index("ix_collections_library_norm", ["library_id", "normalized_name"])


def downgrade() -> None:
    with op.batch_alter_table("collections", naming_convention=NAMING, recreate="always") as batch:
        batch.drop_index("ix_collections_library_norm")
        batch.create_unique_constraint(
            "uq_collections_library_id", ["library_id", "normalized_name"]
        )
