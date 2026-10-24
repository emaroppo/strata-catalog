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


#: Something that wrote to the catalog: an ingest, a prepare, an import, a
#: judge or a backfill. Minted as modelling's runs are, a timestamp and a
#: host token, so either store can name the other's. See docs/adr/0045.
run = Table(
    "run",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("kind", String(16), nullable=False),
    # What ran, and the code identity behind it: package and stage
    # versions, rule-file digests.
    Column("producer", String(255), nullable=False),
    Column("code", JSON, nullable=True),
    # Canonical, never a path: a path says where a file sat, not what it
    # said. docs/adr/0037
    Column("spec", JSON, nullable=True),
    Column("spec_hash", String(64), nullable=True),
    # The spec hash, each stage's version and every rule file's digest: two
    # runs with one chain identity do the same thing to an input.
    Column("chain", String(64), nullable=True),
    # Over the distinct checksums the run was given; the checksums
    # themselves are run_input.
    Column("files_digest", String(64), nullable=True),
    # The run this one supersedes, for the inputs both prepared.
    Column("continues", ForeignKey("run.id"), nullable=True),
    # Per reason, under the names the run's stages chose.
    Column("counts", JSON, nullable=True),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime, nullable=True),
    Index("ix_run_chain", "chain"),
)


#: The distinct checksums a run was handed, after its names resolved. A
#: row per checksum rather than a list on the run: a corpus can be half a
#: million files. See docs/adr/0045.
run_input = Table(
    "run_input",
    metadata,
    Column("run_id", ForeignKey("run.id", ondelete="CASCADE"), primary_key=True),
    Column("checksum", String(64), primary_key=True),
)


#: A file as it arrived. Append-only; a name means one set of bytes. The
#: same bytes under several names are several rows over one blob, so the
#: checksum is not unique. Not a sample: nothing labels a raw file. See
#: docs/adr/0045.
raw = Table(
    "raw",
    metadata,
    Column("id", Integer, primary_key=True),
    # Kept apart from the path, since a collection has segments and a name
    # alone would not say where it ends.
    Column("collection", String(255), nullable=False),
    Column("path", Text, nullable=False),
    Column("checksum", String(64), nullable=False),
    Column("location", Text, nullable=False),
    Column("offset", Integer, nullable=False, default=0),
    Column("length", Integer, nullable=False),
    Column("run_id", ForeignKey("run.id"), nullable=False),
    Column("ingested_at", DateTime, server_default=func.now()),
    UniqueConstraint("collection", "path", name="uq_raw_name"),
    Index("ix_raw_checksum", "checksum"),
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
    # Added and never replaced, so a sample's collections at any point are
    # the rows below an id: what a freeze's watermark covers. docs/adr/0050
    Column("id", Integer, primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), nullable=False),
    Column("collection", String(255), nullable=False),
    # For reading. Null for a membership older than the column.
    Column("added_at", DateTime, server_default=func.now()),
    UniqueConstraint("sample_id", "collection", name="uq_sample_collection"),
    Index("ix_sample_collection_name", "collection"),
)


#: What an origin's input is: a raw file, or another sample.
RAW_INPUT, SAMPLE_INPUT = "raw", "sample"
INPUT_KINDS = (RAW_INPUT, SAMPLE_INPUT)

#: What a sample was made from: one row per input, in each run that made
#: it. The input is named by checksum, so a row means the same in every
#: copy of the catalog. Lineage is the chain of these hops. ``metadata`` is
#: the facts of this one occurrence, as the stages recorded them. See
#: docs/adr/0045 and docs/adr/0049.
origin = Table(
    "origin",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), nullable=False),
    Column("run_id", ForeignKey("run.id"), nullable=False),
    Column("input_kind", String(8), nullable=False),
    Column("input", String(64), nullable=False),
    Column("metadata", JSON, nullable=True),
    Column("recorded_at", DateTime, server_default=func.now()),
    UniqueConstraint("sample_id", "run_id", "input_kind", "input", name="uq_origin_hop"),
    Index("ix_origin_input", "input"),
    Index("ix_origin_run", "run_id"),
)


label_set = Table(
    "label_set",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False, unique=True),
    # A strata.contracts schema, serialized. Append-only by convention.
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
    # A strata.contracts value, serialized. Null when skipped — there is no
    # answer — and an empty value when a human looked and found nothing,
    # which is a real answer and must not be confused with the first.
    Column("value", JSON, nullable=True),
    Column("source", String(16), nullable=False, default="human"),
    # Which import this answer arrived in, carried onto the row that
    # confirms or corrects it. Null for an answer nothing imported.
    # docs/adr/0027
    Column("batch", String(255), nullable=True),
    # The import or judge run that wrote this row. Null for a person's
    # answer, which has no run. docs/adr/0048
    Column("run_id", ForeignKey("run.id"), nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    # Null while this is the current answer. Set when a later row replaced
    # it, or when it was withdrawn — a skip returned to the queue — with
    # no row after it.
    Column("superseded_at", DateTime, nullable=True),
    Index("ix_annotation_current", "sample_id", "label_set_id", "superseded_at"),
    Index("ix_annotation_label_set", "label_set_id", "state", "superseded_at"),
)


#: Derived from ``annotation.value`` on every write, through the indexing
#: contract in strata.contracts. Keyed by the row it was derived from and
#: kept for every row, so what a superseded answer asserted stays readable.
#: The current classes are the current row's. See docs/adr/0039 and
#: docs/adr/0050.
annotation_class = Table(
    "annotation_class",
    metadata,
    Column("annotation_id", ForeignKey("annotation.id", ondelete="CASCADE"), primary_key=True),
    Column("class_name", String(255), primary_key=True),
    Index("ix_annotation_class_name", "class_name"),
)


#: What an event records: a sample tombstoned, a skip withdrawn with
#: nothing after it, a sample's description changed in place, and an
#: answer that arrived already superseded, as history carried from another
#: catalog, so was never the answer here.
TOMBSTONED, WITHDRAWN, DESCRIBED, ARRIVED = "tombstoned", "withdrawn", "described", "arrived"
EVENT_KINDS = (TOMBSTONED, WITHDRAWN, DESCRIBED, ARRIVED)

#: The changes made to a row in place rather than by appending one, each
#: appended here as well. With them, every change a freeze's replay needs
#: is a row with an id under its watermark. ``before`` is what a
#: description was until this event changed it. See docs/adr/0050.
event = Table(
    "event",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), nullable=False),
    # The skip withdrawn, or the answer that arrived superseded.
    Column("annotation_id", ForeignKey("annotation.id", ondelete="CASCADE"), nullable=True),
    Column("before", JSON, nullable=True),
    # For reading; a replay goes by the id.
    Column("at", DateTime, server_default=func.now()),
    Index("ix_event_sample", "sample_id"),
)


#: A set of checksums with a name and a version, which a selection spec
#: includes or excludes: what a script chose, where a condition on one
#: sample cannot say it. A version never changes; a different set under the
#: same name is the next version. Checksums rather than sample ids, since an
#: id means nothing outside its catalog. See docs/adr/0050.
list_version = Table(
    "list_version",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("version", Integer, nullable=False),
    # Over the sorted checksums, so a copy holding the same name and
    # version can be told to hold the same set.
    Column("digest", String(64), nullable=False),
    Column("size", Integer, nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
    UniqueConstraint("name", "version", name="uq_list_version"),
)


list_member = Table(
    "list_member",
    metadata,
    Column("list_id", ForeignKey("list_version.id", ondelete="CASCADE"), primary_key=True),
    Column("checksum", String(64), primary_key=True),
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
    # The selection spec that chose the members, canonical, and its hash.
    # Run again at the watermark, it gives the members back; a round still
    # trains on the rows. Null for a version frozen before specs were
    # recorded, or from samples a caller named. docs/adr/0050
    Column("spec", JSON, nullable=True),
    Column("spec_hash", String(64), nullable=True),
    # How far the catalog had got when the freeze read it: the highest id
    # of each appended table it read.
    Column("watermark", JSON, nullable=True),
    # What the selection counted: candidates by collection and type, what
    # each condition and list removed, which keys were missing.
    Column("selection", JSON, nullable=True),
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
