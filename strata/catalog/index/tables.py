"""The catalog's schema, defined once for both SQLite and Postgres, in SQLAlchemy Core;
a grouping is a metadata key, not a column. See ``docs/adr/0021``, ``docs/adr/0023``.
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

#: How a sample was dealt with; unlabelled is the absence of a row. docs/adr/0009
ANNOTATED = "annotated"
SKIPPED = "skipped"
STATES = (ANNOTATED, SKIPPED)

#: Where an annotation came from; predictions are not annotations. docs/adr/0027
HUMAN, IMPORT, MODEL = "human", "import", "model"
SOURCES = (HUMAN, MODEL, IMPORT)

#: Which source may replace which: never one that outranks the writer. docs/adr/0009
AUTHORITY = {MODEL: 0, IMPORT: 1, HUMAN: 2}


#: Who this catalog is: one row, written once, kept by every copy. docs/adr/0008
catalog_identity = Table(
    "catalog_identity",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("created_at", DateTime, server_default=func.now()),
)


#: Something that wrote to the catalog, minted as modelling's runs are. docs/adr/0045, docs/adr/0053
run = Table(
    "run",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("kind", String(16), nullable=False),
    # What ran, and the code identity behind it
    Column("producer", String(255), nullable=False),
    Column("code", JSON, nullable=True),
    # Canonical, never a path. docs/adr/0037
    Column("spec", JSON, nullable=True),
    Column("spec_hash", String(64), nullable=True),
    # Spec hash, stage versions and rule digests: one chain identity does one thing to an input
    Column("chain", String(64), nullable=True),
    # Over the distinct checksums the run was given, which are run_input
    Column("files_digest", String(64), nullable=True),
    # The run this one supersedes, for the inputs both prepared.
    Column("continues", ForeignKey("run.id"), nullable=True),
    # Per reason, under the names the run's stages chose.
    Column("counts", JSON, nullable=True),
    Column("started_at", DateTime, nullable=False),
    Column("finished_at", DateTime, nullable=True),
    Index("ix_run_chain", "chain"),
)


#: The distinct checksums a run was handed, a row per checksum. docs/adr/0045
run_input = Table(
    "run_input",
    metadata,
    Column("run_id", ForeignKey("run.id", ondelete="CASCADE"), primary_key=True),
    Column("checksum", String(64), primary_key=True),
)


#: A file as it arrived: append-only, one set of bytes per name, nothing labels it. docs/adr/0045
raw = Table(
    "raw",
    metadata,
    Column("id", Integer, primary_key=True),
    # Apart from the path: a collection has segments, so a name alone would not say where it ends
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
    # sha256 of the bytes; with the type, the sample's identity. docs/adr/0052
    Column("checksum", String(64), nullable=False),
    Column("media", String(32), nullable=False),
    Column("subtype", String(32), nullable=False, default="plain"),
    # Subtype-specific and unconstrained; any grouping key lives here. docs/adr/0023
    Column("metadata", JSON, nullable=True),
    Column("ingested_at", DateTime, server_default=func.now()),
    # Removal is a tombstone rather than a delete. docs/adr/0002
    Column("deleted_at", DateTime, nullable=True),
    # Its index leads with the checksum, so it is the lookup by bytes too
    UniqueConstraint("checksum", "media", "subtype", name="uq_sample_identity"),
    Index("ix_sample_subtype", "media", "subtype"),
)

#: A sample's identity and where its bytes are, as every reader that fetches them selects it.
SAMPLE_LOCATION = (
    sample.c.id,
    sample.c.checksum,
    sample.c.media,
    sample.c.subtype,
    sample.c.location,
    sample.c.offset,
    sample.c.length,
)


#: Where a sample came from, as a path, a row per collection; not what it is made of. docs/adr/0022
sample_collection = Table(
    "sample_collection",
    metadata,
    # Added and never replaced, so a freeze's watermark covers a sample's collections. docs/adr/0050
    Column("id", Integer, primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), nullable=False),
    Column("collection", String(255), nullable=False),
    # For reading; null for a membership older than the column
    Column("added_at", DateTime, server_default=func.now()),
    UniqueConstraint("sample_id", "collection", name="uq_sample_collection"),
    Index("ix_sample_collection_name", "collection"),
)


#: What an origin's input is: a raw file, or another sample.
RAW_INPUT, SAMPLE_INPUT = "raw", "sample"
INPUT_KINDS = (RAW_INPUT, SAMPLE_INPUT)

#: What a sample was made from: a row per input per run, by checksum. docs/adr/0045, docs/adr/0049
origin = Table(
    "origin",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), nullable=False),
    Column("run_id", ForeignKey("run.id"), nullable=False),
    Column("input_kind", String(8), nullable=False),
    Column("input", String(64), nullable=False),
    # A sample input's type; with the checksum, which sample. Null for a raw file. docs/adr/0052
    Column("input_type", String(65), nullable=True),
    # The part of a generation the input filled, and its position; null without parts. docs/adr/0053
    Column("role", String(64), nullable=True),
    Column("position", Integer, nullable=True),
    Column("metadata", JSON, nullable=True),
    Column("recorded_at", DateTime, server_default=func.now()),
    Index("ix_origin_input", "input"),
    Index("ix_origin_run", "run_id"),
)

#: One hop is one row; coalesced, since two nulls are distinct to a unique constraint. docs/adr/0053
Index(
    "uq_origin_hop",
    origin.c.sample_id,
    origin.c.run_id,
    origin.c.input_kind,
    origin.c.input,
    func.coalesce(origin.c.input_type, ""),
    func.coalesce(origin.c.role, ""),
    unique=True,
)


#: What made a generated sample: a row per generation. docs/adr/0053, docs/adr/0054
generation = Table(
    "generation",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), nullable=False),
    Column("run_id", ForeignKey("run.id"), nullable=False),
    # The model run and checkpoint that generated it; null for a base model used as it ships
    Column("model_host", String(255), nullable=True),
    Column("model_run", String(64), nullable=True),
    Column("checkpoint", String(255), nullable=True),
    Column("checkpoint_digest", String(64), nullable=True),
    # Each over the canonical JSON of what the run's spec holds; null where none was used
    Column("template_digest", String(64), nullable=True),
    Column("system_digest", String(64), nullable=True),
    Column("settings_digest", String(64), nullable=True),
    Column("values", JSON, nullable=True),
    # The samples a value was copied out of; the model saw the value, so these are not origins
    Column("copied_from", JSON, nullable=True),
    Column("seed", Integer, nullable=True),
    Column("recorded_at", DateTime, server_default=func.now()),
    Index("ix_generation_sample", "sample_id"),
    Index("ix_generation_run", "run_id"),
)


label_set = Table(
    "label_set",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False, unique=True),
    # A strata.contracts schema, serialised; append-only by convention. docs/adr/0005
    Column("schema", JSON, nullable=False),
    Column("created_at", DateTime, server_default=func.now()),
)


#: Append-only: a correction adds a row and stamps the old; the current is unstamped. docs/adr/0027
annotation = Table(
    "annotation",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), nullable=False),
    Column("label_set_id", ForeignKey("label_set.id", ondelete="CASCADE"), nullable=False),
    Column("state", String(16), nullable=False),
    # A strata.contracts value, serialised; null when skipped, and an empty value is a real answer
    Column("value", JSON, nullable=True),
    Column("source", String(16), nullable=False, default="human"),
    # Which import this arrived in, carried onto the row that confirms or corrects it. docs/adr/0027
    Column("batch", String(255), nullable=True),
    # The import or judge run that wrote this row; null for a person's. docs/adr/0048
    Column("run_id", ForeignKey("run.id"), nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    # Null while current; set when replaced, or withdrawn with no row after it
    Column("superseded_at", DateTime, nullable=True),
    Index("ix_annotation_current", "sample_id", "label_set_id", "superseded_at"),
    Index("ix_annotation_label_set", "label_set_id", "state", "superseded_at"),
)

#: An answer as it travels between catalogs: what was said, by whom, and when. docs/adr/0027
ANSWER_COLUMNS = (
    annotation.c.state,
    annotation.c.value,
    annotation.c.source,
    annotation.c.batch,
    annotation.c.created_at,
    annotation.c.superseded_at,
)


#: Derived from ``annotation.value`` on every write, kept per row. docs/adr/0039, docs/adr/0050
annotation_class = Table(
    "annotation_class",
    metadata,
    Column("annotation_id", ForeignKey("annotation.id", ondelete="CASCADE"), primary_key=True),
    Column("class_name", String(255), primary_key=True),
    Index("ix_annotation_class_name", "class_name"),
)


#: What an event records: a tombstone, a withdrawn skip, a changed description, an arrived answer.
TOMBSTONED, WITHDRAWN, DESCRIBED, ARRIVED = "tombstoned", "withdrawn", "described", "arrived"
EVENT_KINDS = (TOMBSTONED, WITHDRAWN, DESCRIBED, ARRIVED)

#: Changes made in place, appended here so a replay finds each under the watermark. docs/adr/0050
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


#: A named, versioned set of checksums, never changed; not ids. docs/adr/0050, docs/adr/0052
list_version = Table(
    "list_version",
    metadata,
    Column("id", Integer, primary_key=True),
    Column("name", String(255), nullable=False),
    Column("version", Integer, nullable=False),
    # Over the sorted checksums, so a copy can be told to hold the same set
    Column("digest", String(64), nullable=False),
    Column("size", Integer, nullable=False),
    # The one type its members are; an untyped list matches on the checksum alone. docs/adr/0052
    Column("media", String(32), nullable=True),
    Column("subtype", String(32), nullable=True),
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
    # A dataset is samples and their annotations against one label set. docs/adr/0024
    Column("label_set_id", ForeignKey("label_set.id"), nullable=False),
    # The spec that chose the members, canonical, and its hash; null if none recorded. docs/adr/0050
    Column("spec", JSON, nullable=True),
    Column("spec_hash", String(64), nullable=True),
    # The highest id of each appended table the freeze read
    Column("watermark", JSON, nullable=True),
    # What the selection counted: candidates, what each condition and list removed, keys missing
    Column("selection", JSON, nullable=True),
    # A digest over the members' annotations; null is unknown, which never matches. docs/adr/0003
    Column("annotation_digest", String(64), nullable=True),
    # Asked for and reached, because grouping can make a target unreachable.
    Column("val_ratio", Float, nullable=False, default=0.2),
    Column("val_ratio_achieved", Float, nullable=False, default=0.0),
    # Zero unless a study asked for one.
    Column("holdout_ratio", Float, nullable=False, default=0.0),
    Column("holdout_ratio_achieved", Float, nullable=False, default=0.0),
    # The metadata key kept on one side when drawn; null means every sample was its own group
    Column("group_by", String(255), nullable=True),
    # The version whose sides this one continues; a re-split names itself. docs/adr/0024
    Column("sides_from_version", Integer, nullable=True),
    # What drew the sides; part of a re-split's identity, not an inheriting version's
    Column("seed", Integer, nullable=True),
    # The split the corpus arrived with, as read; null when every side was drawn. docs/adr/0024
    Column("given_split", JSON, nullable=True),
    # What a freeze did with disagreeing fine-tuning inputs; null with no such type. docs/adr/0053
    Column("disagreeing_inputs", String(8), nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    UniqueConstraint("name", "version"),
)


#: Membership written down, the side a name; version N+1 inherits version N's sides. docs/adr/0003
dataset_member = Table(
    "dataset_member",
    metadata,
    Column("dataset_id", ForeignKey("dataset.id", ondelete="CASCADE"), primary_key=True),
    Column("sample_id", ForeignKey("sample.id", ondelete="CASCADE"), primary_key=True),
    Column("side", String(8), nullable=False, default="train"),
    Index("ix_dataset_member_side", "dataset_id", "side"),
)


#: Two origins that answered one sample differently: a row per sample per label set. docs/adr/0009
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
