"""a sample says what it was made from

Revision ID: c4a81e6d29f5
Revises: 3b9e5d7f1c20

Raw files enter a catalog as rows of their own, samples record an origin
row per input they were made from, and whatever wrote to the catalog is a
run beside what it wrote. An annotation names the run that wrote it, where
one did. See ``docs/adr/0045`` and ``docs/adr/0048``.

Only tables and a nullable column are added, so every existing row reads
as it did; origins for existing samples are a backfill run's job, not this
migration's.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a81e6d29f5"
down_revision: str | None = "3b9e5d7f1c20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("producer", sa.String(length=255), nullable=False),
        sa.Column("code", sa.JSON(), nullable=True),
        sa.Column("spec", sa.JSON(), nullable=True),
        sa.Column("spec_hash", sa.String(length=64), nullable=True),
        sa.Column("chain", sa.String(length=64), nullable=True),
        sa.Column("files_digest", sa.String(length=64), nullable=True),
        sa.Column("continues", sa.String(length=64), sa.ForeignKey("run.id"), nullable=True),
        sa.Column("counts", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_run_chain", "run", ["chain"])
    op.create_table(
        "run_input",
        sa.Column(
            "run_id",
            sa.String(length=64),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("checksum", sa.String(length=64), primary_key=True),
    )
    op.create_table(
        "raw",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("collection", sa.String(length=255), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("offset", sa.Integer(), nullable=False),
        sa.Column("length", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("collection", "path", name="uq_raw_name"),
    )
    op.create_index("ix_raw_checksum", "raw", ["checksum"])
    op.create_table(
        "origin",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sample_id",
            sa.Integer(),
            sa.ForeignKey("sample.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("input_kind", sa.String(length=8), nullable=False),
        sa.Column("input", sa.String(length=64), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("sample_id", "run_id", "input_kind", "input", name="uq_origin_hop"),
    )
    op.create_index("ix_origin_input", "origin", ["input"])
    op.create_index("ix_origin_run", "origin", ["run_id"])
    # Batch mode on SQLite, which cannot add a foreign key in place
    with op.batch_alter_table("annotation") as batch:
        batch.add_column(sa.Column("run_id", sa.String(length=64), nullable=True))
        batch.create_foreign_key("fk_annotation_run_id_run", "run", ["run_id"], ["id"])


def downgrade() -> None:
    """Drops every run, raw row and origin: what they recorded is lost."""
    with op.batch_alter_table("annotation") as batch:
        batch.drop_constraint("fk_annotation_run_id_run", type_="foreignkey")
        batch.drop_column("run_id")
    op.drop_index("ix_origin_run", table_name="origin")
    op.drop_index("ix_origin_input", table_name="origin")
    op.drop_table("origin")
    op.drop_index("ix_raw_checksum", table_name="raw")
    op.drop_table("raw")
    op.drop_table("run_input")
    op.drop_index("ix_run_chain", table_name="run")
    op.drop_table("run")
