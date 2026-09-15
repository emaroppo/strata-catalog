"""The catalog's schema, defined once for both SQLite and Postgres.

SQLAlchemy Core, not the ORM: these are tables and queries, and the rows
they return are data rather than objects with behaviour. See
``docs/adr/0021``.

Two things in here are load-bearing and easy to misread:

``location``/``offset``/``length`` describe where a sample's bytes are. The
local backend writes ``(path relative to the blob root, 0, size)`` — a file
is a shard of one — so that packing samples into tars later is a new backend
rather than a migration.

A grouping — which samples belong together, so a split can keep them on
one side — is a metadata key, not a column. Nothing groups unless a
version is frozen with ``group_by`` naming a key, and the dataset records
which key it respected. See ``docs/adr/0023``.
"""

from sqlalchemy import (
    JSON,
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
#: belong in this table. See docs/adr/0027.
HUMAN, IMPORT, MODEL = "human", "import", "model"
SOURCES = (HUMAN, MODEL, IMPORT)

#: Which source may replace which. A write never replaces an answer from a
#: source that outranks it. See docs/adr/0009.
AUTHORITY = {MODEL: 0, IMPORT: 1, HUMAN: 2}


#: Who this catalog is. One row, written once, kept by every copy. Ids mean
#: nothing outside the catalog that issued them. See docs/adr/0008.
catalog_identity = Table(
    "catalog_identity",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("created_at", DateTime, server_default=func.now()),
)


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
    # Subtype-specific and deliberately unconstrained: frame index, capture
    # time, band count, message id — and any grouping key, such as the
    # video a frame came from.
    Column("metadata", JSON, nullable=True),
    Column("ingested_at", DateTime, server_default=func.now()),
    # Removal is a tombstone rather than a delete. docs/adr/0002
    Column("deleted_at", DateTime, nullable=True),
    Index("ix_sample_subtype", "media", "subtype"),
)


#: Where a sample came from, as a path: ``sat_images``,
#: ``sat_images/2024-batch``. A sample carries one row per collection it
#: belongs to. Distinct from ``media``/``subtype``, which say what a sample
#: is made of, and from any grouping key in its metadata. See docs/adr/0022.
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
    # A strata.labels schema, serialized. Append-only by convention.
    # docs/adr/0005
    Column("schema", JSON, nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
)


#: Append-only. A row is written and never changed; a correction writes a
#: new row and stamps the old one. The current answer for a sample under a
#: label set is the one row with ``superseded_at`` null; unlabelled is
#: having none. See docs/adr/0027.
annotation = Table(
    "annotation",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), nullable=False),
    Column("label_set_id", ForeignKey("label_set.id", ondelete="CASCADE"), nullable=False),
    Column("state", String(16), nullable=False),
    # A strata.labels value, serialized. Null when skipped — there is no
    # answer — and an empty value when a human looked and found nothing,
    # which is a real answer and must not be confused with the first.
    Column("value", JSON, nullable=True),
    Column("source", String(16), nullable=False, default="human"),
    # Which import this answer arrived in, carried onto the row that
    # confirms or corrects it. Null for an answer nothing imported.
    # docs/adr/0027
    Column("batch", String(255), nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    # Null while this is the current answer. Set when a later row replaced
    # it, or when it was withdrawn — a skip returned to the queue — with
    # no row after it.
    Column("superseded_at", DateTime, nullable=True),
    Index("ix_annotation_current", "sample_id", "label_set_id", "superseded_at"),
    Index("ix_annotation_label_set", "label_set_id", "state", "superseded_at"),
)


#: Derived from ``annotation.value`` on every write, through the indexing
#: contract in strata.labels. See docs/adr/0039.
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
    # docs/adr/0024
    Column("label_set_id", ForeignKey("label_set.id"), nullable=False),
    # What selected the members, kept for provenance rather than replayed:
    # re-running it later would return something else, which is the point of
    # materialising in the first place.
    Column("query", JSON, nullable=True),
    # The answers this version froze, as a digest over its members'
    # annotations: a version is samples *and* what was said about them.
    # Null means frozen before this column existed — unknown, and unknown
    # is not a match. See docs/adr/0003.
    Column("annotation_digest", String(64), nullable=True),
    # Asked for and reached, because grouping can make a target unreachable.
    Column("val_ratio", Float, nullable=False, default=0.2),
    Column("val_ratio_achieved", Float, nullable=False, default=0.0),
    # Zero unless a study asked for one.
    Column("holdout_ratio", Float, nullable=False, default=0.0),
    Column("holdout_ratio_achieved", Float, nullable=False, default=0.0),
    # The metadata key whose values were kept on one side when the split
    # was drawn. Null means none: every sample was its own group.
    Column("group_by", String(255), nullable=True),
    # The version whose side assignment this one continues. A version
    # inherits its predecessor's sides and carries this forward; one that
    # re-split from nothing names itself. A warm start never reaches back
    # past it. Null for a version frozen before this was recorded.
    # docs/adr/0024
    Column("sides_from_version", Integer, nullable=True),
    # What drew the sides this version decided. Part of a re-split's
    # identity; not of an inheriting version's.
    Column("seed", Integer, nullable=True),
    # A split the corpus arrived with, as the freeze read it: the metadata
    # key, and which of its values were held out or validation. Null when
    # every side was drawn. See versions/given.py.
    Column("given_split", JSON, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    UniqueConstraint("name", "version"),
)


#: Membership is written down, and version N+1 inherits every side version
#: N decided. The side is a name — ``train``, ``val`` or ``holdout`` — not a
#: flag. See :mod:`strata.catalog.versions.split` and docs/adr/0003.
dataset_member = Table(
    "dataset_member",
    metadata,
    Column("dataset_id", ForeignKey("dataset.id", ondelete="CASCADE"), primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), primary_key=True),
    Column("side", String(8), nullable=False, default="train"),
    Index("ix_dataset_member_side", "dataset_id", "side"),
)


#: Two origins that answered the same sample differently. Not a relaxation
#: of ``annotation``'s key: this records that the one current answer is
#: disputed, alongside it. One row per sample per label set; a third
#: disagreement replaces the second. See docs/adr/0009.
annotation_conflict = Table(
    "annotation_conflict",
    metadata,
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), primary_key=True),
    Column("label_set_id", ForeignKey("label_set.id", ondelete="CASCADE"), primary_key=True),
    #: What the catalog holds, and what arrived disagreeing with it.
    Column("kept_value", JSON, nullable=True),
    Column("other_value", JSON, nullable=True),
    #: Which catalog the disagreeing answer came from.
    Column("other_origin", String(64), nullable=True),
    Column("noticed_at", DateTime, server_default=func.now()),
)
