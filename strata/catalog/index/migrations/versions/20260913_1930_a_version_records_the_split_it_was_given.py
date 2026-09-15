"""a version records the split it was given

Revision ID: 5c2e7a19d4b8
Revises: 6a0d9e3b7c14

A corpus can arrive already divided, and a version may be frozen with
that division fixed: the metadata key naming each sample's set, and which
of its values are held out or validation. The version records what it was
given. Existing versions were given nothing. See ``docs/adr/0024``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5c2e7a19d4b8"
down_revision: str | None = "6a0d9e3b7c14"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.add_column(sa.Column("given_split", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.drop_column("given_split")
