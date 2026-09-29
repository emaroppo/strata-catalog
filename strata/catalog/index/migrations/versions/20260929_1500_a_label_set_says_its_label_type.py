"""a label set says its label type

Revision ID: 3b9e5d7f1c20
Revises: 8e4b2d61f7a3

A label set's schema said ``task`` for what its annotations look like
(``classification``, ``span``, ``bbox``); it says ``label_type`` now, so that
"task" is free for what the annotations are *for* (``docs/adr/0041``). Every
stored schema is rewritten, values unchanged, so the reader no longer has to
accept both.

Rows are read and written through the JSON column (see ``docs/adr/0021``).
Re-running is a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b9e5d7f1c20"
down_revision: str | None = "8e4b2d61f7a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _renamed(old: str, new: str):
    """A schema with ``old`` renamed to ``new``, or None if nothing changes."""

    def convert(schema):
        if not isinstance(schema, dict) or old not in schema:
            return None
        if new in schema:
            raise RuntimeError(
                f"A label set's schema carries both {old!r} and {new!r}; which one "
                f"it means cannot be decided here."
            )
        return {(new if key == old else key): value for key, value in schema.items()}

    return convert


_LABEL_SET = sa.table(
    "label_set",
    sa.column("id", sa.Integer),
    sa.column("schema", sa.JSON),
)


def _rewrite(convert) -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.select(_LABEL_SET.c.id, _LABEL_SET.c.schema)).all()
    updates = [
        {"b_id": set_id, "new": new}
        for set_id, schema in rows
        if (new := convert(schema)) is not None
    ]
    if updates:
        bind.execute(
            sa.update(_LABEL_SET)
            .where(_LABEL_SET.c.id == sa.bindparam("b_id"))
            .values(schema=sa.bindparam("new", type_=sa.JSON)),
            updates,
        )


def upgrade() -> None:
    _rewrite(_renamed("task", "label_type"))


def downgrade() -> None:
    _rewrite(_renamed("label_type", "task"))
