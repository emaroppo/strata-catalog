"""a version records the spec that selected it

Revision ID: e7b2c9a4d103
Revises: c4a81e6d29f5

A dataset version records the selection spec that chose it, its hash and
a watermark, so the spec can be run again where the freeze ran it. For
that every input needs a history: the class index is kept per annotation
row, a collection membership gets an id, and the changes made to a row in
place are appended to an event table as well. Lists of checksums get two
tables. See ``docs/adr/0050``.

The class index is rebuilt from every answer's value, superseded ones
included, through each label set's indexing contract. Existing
memberships get ids in (sample, collection) order and no time: all of
them precede every watermark, so their order means nothing. ``query``
goes; nothing ever wrote it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pydantic import TypeAdapter, ValidationError

from strata.contracts import AnySchema, AnyValue

revision: str = "e7b2c9a4d103"
down_revision: str | None = "c4a81e6d29f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BATCH = 2000


def upgrade() -> None:
    _memberships_get_ids()
    _class_index_per_row()
    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column(
            "sample_id",
            sa.Integer(),
            sa.ForeignKey("sample.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "annotation_id",
            sa.Integer(),
            sa.ForeignKey("annotation.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_event_sample", "event", ["sample_id"])
    op.create_table(
        "list_version",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("digest", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("name", "version", name="uq_list_version"),
    )
    op.create_table(
        "list_member",
        sa.Column(
            "list_id",
            sa.Integer(),
            sa.ForeignKey("list_version.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("checksum", sa.String(length=64), primary_key=True),
    )
    with op.batch_alter_table("dataset") as batch:
        batch.drop_column("query")
        batch.add_column(sa.Column("spec", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("spec_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("watermark", sa.JSON(), nullable=True))
        batch.add_column(sa.Column("selection", sa.JSON(), nullable=True))


def _memberships_get_ids() -> None:
    """A new table under the old name, filled from the old one."""
    op.drop_index("ix_sample_collection_name", table_name="sample_collection")
    op.rename_table("sample_collection", "sample_collection_old")
    if op.get_bind().dialect.name == "postgresql":
        # An index name is the schema's, not the table's, and the new
        # primary key would take this one
        op.execute("ALTER INDEX sample_collection_pkey RENAME TO sample_collection_old_pkey")
    op.create_table(
        "sample_collection",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "sample_id",
            sa.Integer(),
            sa.ForeignKey("sample.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("collection", sa.String(length=255), nullable=False),
        sa.Column("added_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("sample_id", "collection", name="uq_sample_collection"),
    )
    op.execute(
        "INSERT INTO sample_collection (sample_id, collection, added_at) "
        "SELECT sample_id, collection, NULL FROM sample_collection_old "
        "ORDER BY sample_id, collection"
    )
    op.drop_table("sample_collection_old")
    op.create_index("ix_sample_collection_name", "sample_collection", ["collection"])


def _class_index_per_row() -> None:
    """Rebuilt from every answer's value, through its label set's contract.

    A label set whose schema the contract cannot read keeps what the old
    index held, its current answers' classes, rather than losing them.
    """
    op.drop_index("ix_annotation_class_lookup", table_name="annotation_class")
    op.rename_table("annotation_class", "annotation_class_old")
    index = op.create_table(
        "annotation_class",
        sa.Column(
            "annotation_id",
            sa.Integer(),
            sa.ForeignKey("annotation.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("class_name", sa.String(length=255), primary_key=True),
    )

    conn = op.get_bind()
    schemas, values = TypeAdapter(AnySchema), TypeAdapter(AnyValue)
    readable, unreadable = {}, []
    for row in conn.execute(sa.text("SELECT id, schema FROM label_set")):
        try:
            readable[row.id] = schemas.validate_python(_json(row.schema))
        except ValidationError:
            unreadable.append(row.id)

    rows = conn.execute(
        sa.text(
            "SELECT id, label_set_id, value FROM annotation "
            "WHERE state = 'annotated' AND value IS NOT NULL ORDER BY id"
        )
    ).all()
    pending: list[dict] = []
    for row in rows:
        schema = readable.get(row.label_set_id)
        if schema is None:
            continue
        asserted = schema.classes_asserted(values.validate_python(_json(row.value)))
        pending.extend({"annotation_id": row.id, "class_name": name} for name in sorted(asserted))
        if len(pending) >= _BATCH:
            op.bulk_insert(index, pending)
            pending = []
    if pending:
        op.bulk_insert(index, pending)

    for label_set_id in unreadable:
        conn.execute(
            sa.text(
                "INSERT INTO annotation_class (annotation_id, class_name) "
                "SELECT a.id, c.class_name FROM annotation_class_old c "
                "JOIN annotation a ON a.sample_id = c.sample_id "
                "AND a.label_set_id = c.label_set_id AND a.superseded_at IS NULL "
                "WHERE c.label_set_id = :id"
            ),
            {"id": label_set_id},
        )
    op.drop_table("annotation_class_old")
    op.create_index("ix_annotation_class_name", "annotation_class", ["class_name"])


def _json(raw):
    """Postgres JSON through a text() query arrives decoded; SQLite's as the stored text."""
    return json.loads(raw) if isinstance(raw, str) else raw


def downgrade() -> None:
    """Drops the spec, the watermark, every list and every event.

    The class index goes back to current answers only, and memberships lose
    their ids and times.
    """
    with op.batch_alter_table("dataset") as batch:
        batch.drop_column("selection")
        batch.drop_column("watermark")
        batch.drop_column("spec_hash")
        batch.drop_column("spec")
        batch.add_column(sa.Column("query", sa.JSON(), nullable=True))
    op.drop_table("list_member")
    op.drop_table("list_version")
    op.drop_index("ix_event_sample", table_name="event")
    op.drop_table("event")

    op.rename_table("annotation_class", "annotation_class_new")
    op.drop_index("ix_annotation_class_name", table_name="annotation_class_new")
    op.create_table(
        "annotation_class",
        sa.Column("sample_id", sa.Integer(), nullable=False),
        sa.Column("label_set_id", sa.Integer(), nullable=False),
        sa.Column("class_name", sa.String(length=255), nullable=False),
        sa.UniqueConstraint("sample_id", "label_set_id", "class_name"),
    )
    op.execute(
        "INSERT INTO annotation_class (sample_id, label_set_id, class_name) "
        "SELECT a.sample_id, a.label_set_id, c.class_name FROM annotation_class_new c "
        "JOIN annotation a ON a.id = c.annotation_id WHERE a.superseded_at IS NULL"
    )
    op.drop_table("annotation_class_new")
    op.create_index(
        "ix_annotation_class_lookup", "annotation_class", ["label_set_id", "class_name"]
    )

    op.drop_index("ix_sample_collection_name", table_name="sample_collection")
    op.rename_table("sample_collection", "sample_collection_new")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER INDEX sample_collection_pkey RENAME TO sample_collection_new_pkey")
    op.create_table(
        "sample_collection",
        sa.Column("sample_id", sa.Integer(), nullable=False),
        sa.Column("collection", sa.String(length=255), nullable=False),
        sa.ForeignKeyConstraint(["sample_id"], ["sample.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("sample_id", "collection"),
    )
    op.execute(
        "INSERT INTO sample_collection (sample_id, collection) "
        "SELECT sample_id, collection FROM sample_collection_new"
    )
    op.drop_table("sample_collection_new")
    op.create_index("ix_sample_collection_name", "sample_collection", ["collection"])
