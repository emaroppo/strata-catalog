"""The catalog's schema, defined once for both SQLite and Postgres.

SQLAlchemy Core, not the ORM: these are tables and queries, and the rows
they return are data rather than objects with behaviour. The behaviour lives
in :mod:`strata.labels`, which is where a value knows what it means.

Two things in here are load-bearing and easy to misread:

``location``/``offset``/``length`` describe where a sample's bytes are. The
local backend writes ``(path relative to the blob root, 0, size)`` — a file
is a shard of one — so that packing samples into tars later is a new backend
rather than a migration.

``group_id`` is what keeps near-duplicates on one side of a train/val split.
Null means the sample is its own group, which is the ordinary case; frames
of one video share a value. Because it is per-sample data rather than
configuration, one catalog holds standalone images and video frames at once.
"""

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

metadata = MetaData()

#: An annotation row exists because somebody dealt with the sample; the
#: state says how. Unlabelled is the absence of a row, so there is no
#: combination of flags that means nothing.
ANNOTATED = "annotated"
SKIPPED = "skipped"
STATES = (ANNOTATED, SKIPPED)

#: Where an annotation came from. Predictions are not annotations and do not
#: belong in this table, but an imported guess that a human then accepted is
#: worth telling apart from one typed from scratch.
SOURCES = ("human", "model", "import")


sample = Table(
    "sample",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("location", Text, nullable=False),
    Column("offset", Integer, nullable=False, default=0),
    Column("length", Integer, nullable=False),
    # sha256 of the bytes. Unique, so ingesting the same file twice under
    # two names is one sample rather than two.
    Column("checksum", String(64), nullable=False, unique=True),
    Column("media", String(32), nullable=False),
    Column("subtype", String(32), nullable=False, default="plain"),
    Column("group_id", String(255), nullable=True),
    # Subtype-specific and deliberately unconstrained: frame index, capture
    # time, band count, message id.
    Column("metadata", JSON, nullable=True),
    Column("ingested_at", DateTime, server_default=func.now()),
    # Shards are immutable, so removal is a tombstone and a compaction pass
    # later rather than a delete.
    Column("deleted_at", DateTime, nullable=True),
    Index("ix_sample_group", "group_id"),
    Index("ix_sample_subtype", "media", "subtype"),
)


#: Where a sample came from, as a path: ``sat_images``,
#: ``sat_images/2024-batch``. A sample carries one row per collection it
#: belongs to, because the same images can feed more than one job — which is
#: the whole reason a catalog is worth keeping.
#:
#: Distinct from ``media``/``subtype``, which say what a sample is made of,
#: and from ``group_id``, which says what must not straddle a split. One
#: collection holds many groups; the three axes are independent.
sample_collection = Table(
    "sample_collection",
    metadata,
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), primary_key=True),
    Column("collection", String(255), primary_key=True),
    Index("ix_sample_collection_name", "collection"),
)


label_set = Table(
    "label_set",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False, unique=True),
    # A strata.labels schema, serialized. Append-only by convention: a
    # checkpoint maps output neurons to the class list by position, so a run
    # records the list it trained with rather than trusting this to hold still.
    Column("schema", JSON, nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
)


annotation = Table(
    "annotation",
    metadata,
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), primary_key=True),
    Column("label_set_id", ForeignKey("label_set.id", ondelete="CASCADE"), primary_key=True),
    Column("state", String(16), nullable=False),
    # A strata.labels value, serialized. Null when skipped — there is no
    # answer — and an empty value when a human looked and found nothing,
    # which is a real answer and must not be confused with the first.
    Column("value", JSON, nullable=True),
    Column("source", String(16), nullable=False, default="human"),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
    Index("ix_annotation_label_set", "label_set_id", "state"),
)


#: Derived from ``annotation.value`` on every write, through the indexing
#: contract in strata.labels. Its whole purpose is to turn "every sample
#: labelled X" into a join instead of a scan over JSON, and it is why a new
#: task type becomes queryable without the catalog understanding it.
annotation_class = Table(
    "annotation_class",
    metadata,
    Column("sample_id", Integer, nullable=False),
    Column("label_set_id", Integer, nullable=False),
    Column("class_name", String(255), nullable=False),
    UniqueConstraint("sample_id", "label_set_id", "class_name"),
    Index("ix_annotation_class_lookup", "label_set_id", "class_name"),
)


dataset = Table(
    "dataset",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("version", Integer, nullable=False),
    # A dataset is samples *and their annotations against one label set*.
    # Without this, materialising would have to be told which it meant.
    Column("label_set_id", ForeignKey("label_set.id"), nullable=False),
    # What selected the members, kept for provenance rather than replayed:
    # re-running it later would return something else, which is the point of
    # materialising in the first place.
    Column("query", JSON, nullable=True),
    # Both, because grouping can make the target unreachable: a corpus of
    # two videos cannot hold out 20% of itself, and a caller that asked for
    # 20% and got 50% should be able to find that out afterwards.
    Column("val_ratio", Float, nullable=False, default=0.2),
    Column("val_ratio_achieved", Float, nullable=False, default=0.0),
    Column("created_at", DateTime, server_default=func.now()),
    UniqueConstraint("name", "version"),
)


#: Membership is written down rather than recomputed. That is what makes the
#: train/val split stable: version N+1 inherits every shared sample's side
#: and assigns only what is new, so a warm-started model is never scored on
#: something an earlier round trained it on.
dataset_member = Table(
    "dataset_member",
    metadata,
    Column("dataset_id", ForeignKey("dataset.id", ondelete="CASCADE"), primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), primary_key=True),
    Column("val", Boolean, nullable=False, default=False),
    Index("ix_dataset_member_val", "dataset_id", "val"),
)
