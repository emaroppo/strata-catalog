"""a dataset version holds out

Revision ID: 7d3f0c1a9b2e
Revises: 51e400c6605f

A member's side was a flag, val or not, and a flag has no room for a third.
It becomes a name — train, val or holdout — so a held-out sample cannot be
read as "not validation" and trained on. The dataset records the holdout
ratio it was asked for and the one it reached, beside val's.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7d3f0c1a9b2e"
down_revision: str | None = "51e400c6605f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("holdout_ratio", sa.Float(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("holdout_ratio_achieved", sa.Float(), nullable=False, server_default="0")
        )

    with op.batch_alter_table("dataset_member", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("side", sa.String(length=8), nullable=False, server_default="train")
        )
    op.execute("UPDATE dataset_member SET side = 'val' WHERE val")
    with op.batch_alter_table("dataset_member", schema=None) as batch_op:
        batch_op.drop_index("ix_dataset_member_val")
        batch_op.drop_column("val")
        batch_op.create_index("ix_dataset_member_side", ["dataset_id", "side"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("dataset_member", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("val", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.execute("UPDATE dataset_member SET val = (side = 'val')")
    with op.batch_alter_table("dataset_member", schema=None) as batch_op:
        batch_op.drop_index("ix_dataset_member_side")
        batch_op.drop_column("side")
        batch_op.create_index("ix_dataset_member_val", ["dataset_id", "val"], unique=False)

    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.drop_column("holdout_ratio_achieved")
        batch_op.drop_column("holdout_ratio")
