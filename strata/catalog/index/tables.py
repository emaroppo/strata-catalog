"""The catalog's schema, defined once for both SQLite and Postgres.

SQLAlchemy Core, not the ORM: these are tables and queries, and the rows
they return are data rather than objects with behaviour. The behaviour lives
in :mod:`strata.labels`, which is where a value knows what it means.

Two things in here are load-bearing and easy to misread:

``location``/``offset``/``length`` describe where a sample's bytes are. The
local backend writes ``(path relative to the blob root, 0, size)`` — a file
is a shard of one — so that packing samples into tars later is a new backend
rather than a migration.

A grouping — which samples belong together, so a split can keep them on
one side — is a metadata key, not a column. Frames carry ``video``, mail
may carry ``thread``, and a project can write any key of its own. Nothing
groups unless a version is frozen with ``group_by`` naming a key, and one
catalog can be split by several groupings in turn. The dataset records
which key it respected.
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
#: belong in this table, but an imported guess that a human then accepted is
#: worth telling apart from one typed from scratch.
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
    # Shards are immutable, so removal is a tombstone and a compaction pass
    # later rather than a delete.
    Column("deleted_at", DateTime, nullable=True),
    Index("ix_sample_subtype", "media", "subtype"),
)


#: Where a sample came from, as a path: ``sat_images``,
#: ``sat_images/2024-batch``. A sample carries one row per collection it
#: belongs to, because the same images can feed more than one job — which is
#: the whole reason a catalog is worth keeping.
#:
#: Distinct from ``media``/``subtype``, which say what a sample is made of,
#: and from any grouping key in its metadata. One collection holds many
#: groups; the axes are independent.
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
    # past it: a model trained before a re-split may have seen what is now
    # held out. Null for a version frozen before this was recorded.
    Column("sides_from_version", Integer, nullable=True),
    # What drew the sides this version decided. Part of a re-split's
    # identity, since the draw is the whole of what it did; not of an
    # inheriting version's, where it reaches only the samples that are new.
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


#: Two origins that answered the same sample differently.
#:
#: A merge cannot decide this. Both answers were made by someone looking at
#: the sample, so one of them is a mistake and only a person can say which —
#: and the useful thing to show that person is *what* disagreed, which is
#: lost the moment either answer is discarded.
#:
#: Not a relaxation of ``annotation``'s key. One current answer per sample
#: per label set is worth keeping: everything that reads an annotation wants
#: the answer, not a set of candidates. This records that the answer is
#: disputed, alongside it.
#:
#: One row per sample per label set. A third disagreement replaces the
#: second — the pair being shown matters more than the history of who
#: disagreed when, and the history is in the origins anyway.
annotation_conflict = Table(
    "annotation_conflict",
    metadata,
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), primary_key=True),
    Column("label_set_id", ForeignKey("label_set.id", ondelete="CASCADE"), primary_key=True),
    #: What the catalog holds, and what arrived disagreeing with it.
    Column("kept_value", JSON, nullable=True),
    Column("other_value", JSON, nullable=True),
    #: Which catalog the disagreeing answer came from, so "the laptop said
    #: otherwise" is answerable rather than merely "something did".
    Column("other_origin", String(64), nullable=True),
    Column("noticed_at", DateTime, server_default=func.now()),
)
