"""a grouping is a metadata key

Revision ID: 2f8c41b6e0d9
Revises: 103841d7ceaf

``sample.group_id`` was one grouping, filled at ingest by the sample
type's rule and respected by every split. A grouping is now a metadata
key, and a version is frozen with ``group_by`` naming the one it respects,
or none. Every existing group id moves into the sample's metadata under
the key its type would write today; a version frozen before this records
no ``group_by``. See ``docs/adr/0023``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2f8c41b6e0d9"
down_revision: str | None = "103841d7ceaf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SAMPLE = sa.table(
    "sample",
    sa.column("id", sa.Integer),
    sa.column("subtype", sa.String),
    sa.column("group_id", sa.String),
    sa.column("metadata", sa.JSON),
)


def _key_for(subtype: str) -> str:
    """The metadata key a sample type writes its grouping under."""
    if subtype.startswith("frames"):
        return "video"
    if "email" in subtype:
        return "thread"
    return "group"


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, str):
        return json.loads(value) or {}
    return dict(value)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(_SAMPLE.c.id, _SAMPLE.c.subtype, _SAMPLE.c.group_id, _SAMPLE.c.metadata).where(
            _SAMPLE.c.group_id.is_not(None)
        )
    ).all()
    for sample_id, subtype, group_id, metadata in rows:
        moved = {**_as_dict(metadata), _key_for(subtype or ""): group_id}
        bind.execute(sa.update(_SAMPLE).where(_SAMPLE.c.id == sample_id).values(metadata=moved))
    with op.batch_alter_table("sample", schema=None) as batch_op:
        batch_op.drop_index("ix_sample_group")
        batch_op.drop_column("group_id")
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.add_column(sa.Column("group_by", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("dataset", schema=None) as batch_op:
        batch_op.drop_column("group_by")
    with op.batch_alter_table("sample", schema=None) as batch_op:
        batch_op.add_column(sa.Column("group_id", sa.String(length=255), nullable=True))
        batch_op.create_index("ix_sample_group", ["group_id"], unique=False)
    bind = op.get_bind()
    rows = bind.execute(sa.select(_SAMPLE.c.id, _SAMPLE.c.subtype, _SAMPLE.c.metadata)).all()
    for sample_id, subtype, metadata in rows:
        value = _as_dict(metadata).get(_key_for(subtype or ""))
        if value is not None:
            bind.execute(
                sa.update(_SAMPLE).where(_SAMPLE.c.id == sample_id).values(group_id=str(value))
            )
