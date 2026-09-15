"""an annotation keeps its history

Revision ID: 8e4b2d61f7a3
Revises: 5c2e7a19d4b8

An annotation was one row per sample per label set, updated in place. It
becomes append-only: a row is written and never changed, a correction
writes a new row and stamps the old one as superseded, and the current
answer is the one row not stamped. Every existing row becomes the first
and only version of itself. A row also records which import batch it
arrived in, or descends from. See ``docs/adr/0027``.

The table is rebuilt rather than altered, since the primary key changes
from the pair to a row id (see ``docs/adr/0021``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e4b2d61f7a3"
down_revision: str | None = "5c2e7a19d4b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "annotation_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sample_id",
            sa.Integer(),
            sa.ForeignKey("sample.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "label_set_id",
            sa.Integer(),
            sa.ForeignKey("label_set.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("batch", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        "INSERT INTO annotation_history "
        "(sample_id, label_set_id, state, value, source, created_at) "
        "SELECT sample_id, label_set_id, state, value, source, updated_at FROM annotation"
    )
    op.drop_index("ix_annotation_label_set", table_name="annotation")
    op.drop_table("annotation")
    op.rename_table("annotation_history", "annotation")
    op.create_index(
        "ix_annotation_current", "annotation", ["sample_id", "label_set_id", "superseded_at"]
    )
    op.create_index(
        "ix_annotation_label_set", "annotation", ["label_set_id", "state", "superseded_at"]
    )


def downgrade() -> None:
    """Back to one row per sample and label set: the current answer, history dropped."""
    op.create_table(
        "annotation_cell",
        sa.Column(
            "sample_id",
            sa.Integer(),
            sa.ForeignKey("sample.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "label_set_id",
            sa.Integer(),
            sa.ForeignKey("label_set.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO annotation_cell (sample_id, label_set_id, state, value, source, updated_at) "
        "SELECT sample_id, label_set_id, state, value, source, created_at FROM annotation "
        "WHERE superseded_at IS NULL"
    )
    op.drop_index("ix_annotation_label_set", table_name="annotation")
    op.drop_index("ix_annotation_current", table_name="annotation")
    op.drop_table("annotation")
    op.rename_table("annotation_cell", "annotation")
    op.create_index("ix_annotation_label_set", "annotation", ["label_set_id", "state"])
