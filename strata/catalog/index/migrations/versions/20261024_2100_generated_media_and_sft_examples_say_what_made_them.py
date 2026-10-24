"""generated media and sft examples say what made them

Revision ID: c7f6ec466c61
Revises: e83c65fe4277

An origin row gains the input's type, for a sample input, and the role
and position it filled in a generation. The role and the type join the
row's uniqueness, which becomes a unique index over the nullable columns
as they read, since two nulls are distinct to a constraint. A
``generation`` table records what made a generated sample besides its
inputs, and a dataset version records what its freeze did with a
fine-tuning sample whose origins disagree on its input. See
``docs/adr/0053``.

Only columns and a table are added, so every existing row reads as it
did: an origin with no type, role or position, and a version with no
option, which is what one with no fine-tuning type records.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7f6ec466c61"
down_revision: str | None = "e83c65fe4277"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HOP = ["sample_id", "run_id", "input_kind", "input"]


def upgrade() -> None:
    with op.batch_alter_table("origin") as batch:
        batch.drop_constraint("uq_origin_hop", type_="unique")
        batch.add_column(sa.Column("input_type", sa.String(length=65), nullable=True))
        batch.add_column(sa.Column("role", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("position", sa.Integer(), nullable=True))
    op.create_index(
        "uq_origin_hop",
        "origin",
        [*_HOP, sa.text("coalesce(input_type, '')"), sa.text("coalesce(role, '')")],
        unique=True,
    )
    op.create_table(
        "generation",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sample_id",
            sa.Integer(),
            sa.ForeignKey("sample.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(length=64), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("model_host", sa.String(length=255), nullable=True),
        sa.Column("model_run", sa.String(length=64), nullable=True),
        sa.Column("checkpoint", sa.String(length=255), nullable=True),
        sa.Column("checkpoint_digest", sa.String(length=64), nullable=True),
        sa.Column("template_digest", sa.String(length=64), nullable=True),
        sa.Column("system_digest", sa.String(length=64), nullable=True),
        sa.Column("settings_digest", sa.String(length=64), nullable=True),
        sa.Column("values", sa.JSON(), nullable=True),
        sa.Column("copied_from", sa.JSON(), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_generation_sample", "generation", ["sample_id"])
    op.create_index("ix_generation_run", "generation", ["run_id"])
    with op.batch_alter_table("dataset") as batch:
        batch.add_column(sa.Column("disagreeing_inputs", sa.String(length=8), nullable=True))


def downgrade() -> None:
    """Drops every generation record, and each origin's type, role and position.

    Refused while two origins differ only in those: one sample filling
    two roles in one run, or the same bytes as two types given as one
    input. Before this revision the two would be one row, and which to
    keep is not ours.
    """
    shared = (
        op.get_bind()
        .execute(
            sa.text(
                f"SELECT {', '.join(_HOP)} FROM origin GROUP BY {', '.join(_HOP)} "
                f"HAVING COUNT(*) > 1"
            )
        )
        .first()
    )
    if shared is not None:
        raise RuntimeError(
            f"Sample {shared.sample_id} has several origins in run {shared.run_id} from "
            f"{shared.input[:12]}, told apart by their role or the input's type. Before "
            f"this revision they would be one row, so the catalog cannot go back past it "
            f"as it stands."
        )
    with op.batch_alter_table("dataset") as batch:
        batch.drop_column("disagreeing_inputs")
    op.drop_index("ix_generation_run", table_name="generation")
    op.drop_index("ix_generation_sample", table_name="generation")
    op.drop_table("generation")
    op.drop_index("uq_origin_hop", table_name="origin")
    with op.batch_alter_table("origin") as batch:
        batch.drop_column("position")
        batch.drop_column("role")
        batch.drop_column("input_type")
        batch.create_unique_constraint("uq_origin_hop", _HOP)
