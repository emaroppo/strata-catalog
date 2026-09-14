"""a span carries labels

Revision ID: 103841d7ceaf
Revises: 7d3f0c1a9b2e

A span was written with one ``label``; it is read with ``labels``, a list,
since a region may carry more than one. Every stored span is rewritten to
the list form so the reader no longer has to accept both. An empty label
becomes no labels: it was how a region sent without one was represented,
and an empty string is not a class name.

Rows are read and written through the JSON column so the same code runs on
SQLite and Postgres. Re-running is a no-op.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "103841d7ceaf"
down_revision: str | None = "7d3f0c1a9b2e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _to_labels(value):
    """The list form of a spans value, or None if nothing changes."""
    if not isinstance(value, dict) or value.get("kind") != "spans":
        return None
    changed = False
    spans = []
    for span in value.get("values") or []:
        if isinstance(span, dict) and "label" in span:
            span = dict(span)
            lone = span.pop("label")
            span.setdefault("labels", [lone] if lone else [])
            changed = True
        spans.append(span)
    if not changed:
        return None
    return {**value, "values": spans}


def _to_label(value):
    """The single-label form, or None if nothing changes. Refuses two labels."""
    if not isinstance(value, dict) or value.get("kind") != "spans":
        return None
    changed = False
    spans = []
    for span in value.get("values") or []:
        if isinstance(span, dict) and "labels" in span:
            span = dict(span)
            labels = span.pop("labels")
            if len(labels) > 1:
                raise RuntimeError(
                    "A span carries more than one label, which the single-label "
                    "form cannot hold; this migration cannot be walked back."
                )
            span["label"] = labels[0] if labels else ""
            changed = True
        spans.append(span)
    if not changed:
        return None
    return {**value, "values": spans}


_ANNOTATION = sa.table(
    "annotation",
    sa.column("sample_id", sa.Integer),
    sa.column("label_set_id", sa.Integer),
    sa.column("value", sa.JSON),
)
_CONFLICT = sa.table(
    "annotation_conflict",
    sa.column("sample_id", sa.Integer),
    sa.column("label_set_id", sa.Integer),
    sa.column("kept_value", sa.JSON),
    sa.column("other_value", sa.JSON),
)


def _keyed(table):
    """Both tables are keyed on the sample and the label set."""
    return (table.c.sample_id == sa.bindparam("b_sample")) & (
        table.c.label_set_id == sa.bindparam("b_set")
    )


def _rewrite(convert) -> None:
    bind = op.get_bind()

    rows = bind.execute(
        sa.select(_ANNOTATION.c.sample_id, _ANNOTATION.c.label_set_id, _ANNOTATION.c.value)
    ).all()
    updates = [
        {"b_sample": sample_id, "b_set": set_id, "new": new}
        for sample_id, set_id, value in rows
        if (new := convert(value)) is not None
    ]
    if updates:
        bind.execute(
            sa.update(_ANNOTATION)
            .where(_keyed(_ANNOTATION))
            .values(value=sa.bindparam("new", type_=sa.JSON)),
            updates,
        )

    rows = bind.execute(
        sa.select(
            _CONFLICT.c.sample_id,
            _CONFLICT.c.label_set_id,
            _CONFLICT.c.kept_value,
            _CONFLICT.c.other_value,
        )
    ).all()
    updates = []
    for sample_id, set_id, kept, other in rows:
        new_kept, new_other = convert(kept), convert(other)
        if new_kept is None and new_other is None:
            continue
        updates.append(
            {
                "b_sample": sample_id,
                "b_set": set_id,
                "kept": kept if new_kept is None else new_kept,
                "other": other if new_other is None else new_other,
            }
        )
    if updates:
        bind.execute(
            sa.update(_CONFLICT)
            .where(_keyed(_CONFLICT))
            .values(
                kept_value=sa.bindparam("kept", type_=sa.JSON),
                other_value=sa.bindparam("other", type_=sa.JSON),
            ),
            updates,
        )


def upgrade() -> None:
    _rewrite(_to_labels)


def downgrade() -> None:
    _rewrite(_to_label)
