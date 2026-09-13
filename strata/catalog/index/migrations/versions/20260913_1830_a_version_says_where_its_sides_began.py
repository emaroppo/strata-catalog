"""a version says where its sides began

Revision ID: 6a0d9e3b7c14
Revises: 2f8c41b6e0d9

A version inherits its predecessor's sides, and a warm start follows the
dataset name across versions on the strength of that: a model carried from
v3 to v4 has never seen v4's validation or holdout. A version may now
re-split from nothing, and records the version its sides descend from, so
a warm start stops there rather than continuing a model that trained on
what is now held out. Existing versions record nothing, which reads as
"inherits as before" — no boundary — and is what was true of them. The
seed that drew the sides is recorded too, since for a re-split the draw
is the whole of what the version did.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6a0d9e3b7c14"
down_revision: str | None = "2f8c41b6e0d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.add_column(sa.Column("sides_from_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("seed", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.drop_column("seed")
        batch_op.drop_column("sides_from_version")
