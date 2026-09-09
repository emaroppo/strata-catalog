"""The catalog itself: ingest, annotate, query, materialise.

Everything a caller needs goes through here, so the index and the blob
backend stay implementation details. That is what lets SQLite and a
directory be swapped for Postgres and a bucket without a consumer noticing.
"""

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import TypeAdapter
from sqlalchemy import (
    and_,
    create_engine,
    delete,
    event,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine

from strata.labels import AnySchema, AnyValue

from . import tables as t
from .blobs import BlobBackend, LocalBackend, Location, blob_path, checksum_of
from .manifest import FILES_DIR, MANIFEST_NAME, Manifest, ManifestSample
from .split import assign

_SCHEMA = TypeAdapter(AnySchema)
_VALUE = TypeAdapter(AnyValue)


def _chunks(items: list, size: int):
    """SQLite caps parameters per statement, and a review pool is past it."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _canonical_source(
    path: Path, canonicalise: Callable[[bytes], bytes], scratch: Path, entry: dict | None
) -> tuple[Path, dict | None]:
    """The file to catalogue, and what to record if it is not the one given.

    A file already in canonical form is catalogued as it stands, which keeps
    the hardlink path and means a corpus prepared properly pays nothing for
    this. Where the bytes do differ, the canonical ones are what the catalog
    holds — but the original's checksum is recorded, so a sample can still
    be traced to the file on disk it came from.
    """
    data = path.read_bytes()
    try:
        canonical = canonicalise(data)
    except Exception as e:
        # Broad on purpose: this is a sample type's code, it may raise
        # anything, and the one thing missing from whatever it raised is
        # which of forty thousand files it was looking at.
        raise CatalogError(f"Cannot canonicalise {path}: {e}") from None
    if canonical == data:
        return path, entry

    source_checksum = hashlib.sha256(data).hexdigest()
    target = scratch / f"{source_checksum}{path.suffix.lower()}"
    target.write_bytes(canonical)
    return target, {
        **(entry or {}),
        "canonicalised": True,
        "source_checksum": source_checksum,
    }


def _link_or_copy(source: Path, target: Path) -> None:
    """Put ``source``'s bytes at ``target``, sharing them if the filesystem can.

    A blob is immutable and addressed by its content, and anything derived
    from it is the same bytes — so the two can share an inode. Without this
    every dataset version costs a full copy of itself, and a project of any
    size runs out of disk.
    """
    try:
        os.link(source, target)
    except OSError:
        # A different filesystem, or one that will not link. Correctness
        # does not depend on the link, only disk usage.
        shutil.copyfile(source, target)


def _materialised_name(row) -> str:
    """What a sample is called inside a materialised dataset.

    Its checksum, not its container. A container was one file when blobs
    were files, so naming after it happened to be unique; a tar shard holds
    hundreds, and naming after it gave every sample in the shard the same
    filename — one file on disk, every manifest entry pointing at it, and a
    training run over one image repeated with nothing to say so.

    The extension comes from the recorded source path, because a checksum
    has none and some readers still look.
    """
    suffix = Path((row.metadata or {}).get("source_path") or "").suffix
    return f"{row.checksum}{suffix.lower()}"


#: Every collection, said out loud. A query has to name what it wants —
#: forgetting to scope one is how a project's review queue fills with
#: another project's data — so this exists to make "all of it" a thing you
#: choose rather than a thing you get by omission.
EVERYTHING = "*"


def _within(collections) -> object | None:
    """A condition matching samples in any of ``collections``.

    ``None`` when the answer is everything, so a caller can drop the join
    entirely rather than filter on a tautology.

    Selecting a collection selects what is under it: ``sat_images`` covers
    ``sat_images/2024`` but never ``sat_images_old``, which a bare prefix
    match would swallow. That distinction lives here rather than at each
    call site.
    """
    if collections == EVERYTHING:
        return None
    names = [collections] if isinstance(collections, str) else list(collections)
    if not names:
        raise CatalogError(
            "No collections given. Name what to draw from, or pass EVERYTHING "
            "to mean the whole catalog — an empty list would silently be one "
            "or the other."
        )
    return or_(
        *[
            or_(
                t.sample_collection.c.collection == name,
                t.sample_collection.c.collection.like(f"{name}/%"),
            )
            for name in names
        ]
    )


class CatalogError(Exception):
    """A request the catalog cannot honour."""


@dataclass(frozen=True)
class SampleRow:
    """A sample as callers see it — never a raw database row."""

    id: int
    checksum: str
    location: Location
    media: str
    subtype: str
    group_id: str | None
    #: Out of comparison, and therefore out of the generated hash. A frozen
    #: dataclass hashes every field it compares, and a dict cannot be
    #: hashed — so a row carrying metadata could not be put in a set or used
    #: as a key at all. It stayed usable only while every sample had none.
    #: Excluding it is also the truer reading: two rows for the same sample
    #: are the same sample, whatever is recorded about where it came from.
    metadata: dict | None = field(default=None, compare=False)


class Catalog:
    """Samples, what is known about them, and the datasets built from them."""

    def __init__(self, engine: Engine, blobs: BlobBackend):
        self.engine = engine
        self.blobs = blobs

    @classmethod
    def local(cls, root: Path) -> "Catalog":
        """A catalog needing no infrastructure: SQLite beside a blob directory.

        The reason the repository stays runnable by someone who just cloned
        it, and the same schema and queries as the server configuration.
        """
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        return cls.connect(f"sqlite:///{root / 'catalog.db'}", LocalBackend(root / "blobs"))

    @classmethod
    def connect(cls, url: str, blobs: BlobBackend) -> "Catalog":
        """A catalog on any index the same schema runs against.

        One schema, two dialects: SQLite for a checkout with nothing
        installed, Postgres once the corpus is millions of rows read from
        several machines at once, which is where SQLite stops being the
        right answer.
        """
        engine = create_engine(url)
        if engine.dialect.name == "sqlite":

            @event.listens_for(engine, "connect")
            def _pragmas(dbapi_connection, _record):
                # A full fsync per commit is what makes a bulk import crawl,
                # and this index is rebuildable from the blobs and the source
                # it came from. WAL also lets a reader run during an import.
                # Neither has an equivalent worth setting on Postgres.
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()

        catalog = cls(engine, blobs)
        catalog.create_all()
        return catalog

    def create_all(self) -> None:
        t.metadata.create_all(self.engine)
        self._mint_identity()

    def _mint_identity(self) -> None:
        """Give a catalog its name when it comes into existence.

        Not on first ask. A catalog nobody had questioned would have no
        identity, and a copy of it would carry none — so whether two
        databases are the same corpus would depend on whether anyone had
        happened to look.
        """
        with self.engine.begin() as conn:
            if conn.execute(select(t.catalog_identity.c.id)).scalar() is not None:
                return
            conn.execute(
                insert(t.catalog_identity).values(
                    id=f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
                    f"-{uuid.uuid4().hex[:8]}"
                )
            )

    @property
    def id(self) -> str:
        """Which catalog this is. Sortable by time, unique without coordination."""
        with self.engine.connect() as conn:
            return conn.execute(select(t.catalog_identity.c.id)).scalar()

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(
        self,
        paths: Iterable[Path],
        media: str,
        subtype: str = "plain",
        group_id: str | None = None,
        metadata: dict | None = None,
        metadata_for: Callable[[Path], dict | None] | None = None,
        collections: Iterable[str] = (),
        on_sample: Callable[[Path], None] | None = None,
        canonicalise: Callable[[bytes], bytes] | None = None,
    ) -> list[int]:
        """Register files, storing their bytes and returning their sample ids.

        Idempotent on content: a file whose bytes are already catalogued
        returns the existing id rather than a duplicate, which is what makes
        re-running ingest over a growing directory safe.

        ``metadata`` applies to the whole batch; ``metadata_for`` supplies it
        per file, for anything that differs sample by sample — where a file
        came from, its frame index, its capture time.

        A known sample has its ``subtype``, ``group_id`` and ``metadata``
        brought up to date rather than left alone, so correcting how a
        corpus is described is one re-run rather than a rebuild. Regrouping
        cannot disturb a dataset already built: membership is materialised
        into ``dataset_member``, so only later versions see the change.

        ``collections`` says where these samples came from, as paths. They
        are added rather than replaced: a sample already in one collection
        that turns up in another belongs to both, which is what makes a
        corpus reusable across jobs rather than owned by the first.

        ``canonicalise`` is a sample type's canonical form, passed in rather
        than resolved here so that a catalog stays free of the type registry.
        Where it changes a file's bytes, those are what is stored and what
        the checksum addresses; the sample records ``canonicalised`` and the
        original's ``source_checksum``. Omitted — which is every media whose
        type does not override it — no file is read at all.

        The whole batch is one transaction — a commit per file costs an
        fsync each and turns an import into a crawl — so ``on_sample`` is how
        a caller reports progress without breaking that up.
        """
        ids: list[int] = []
        with TemporaryDirectory(prefix="strata-canonical-") as scratch, (
            self.engine.begin()
        ) as conn:
            for path in paths:
                path = Path(path)
                extra = metadata_for(path) if metadata_for is not None else None
                entry = {**(metadata or {}), **(extra or {})} or None
                source = path
                if canonicalise is not None:
                    source, entry = _canonical_source(
                        path, canonicalise, Path(scratch), entry
                    )
                checksum = checksum_of(source)
                existing = conn.execute(
                    select(t.sample.c.id).where(t.sample.c.checksum == checksum)
                ).scalar_one_or_none()
                if existing is not None:
                    conn.execute(
                        update(t.sample)
                        .where(t.sample.c.id == existing)
                        .values(subtype=subtype, group_id=group_id, metadata=entry)
                    )
                    ids.append(existing)
                    if on_sample is not None:
                        on_sample(path)
                    continue
                location = self.blobs.put(source, checksum)
                ids.append(
                    conn.execute(
                        insert(t.sample).values(
                            location=location.container,
                            offset=location.offset,
                            length=location.length,
                            checksum=checksum,
                            media=media,
                            subtype=subtype,
                            group_id=group_id,
                            metadata=entry,
                        )
                    ).inserted_primary_key[0]
                )
                if on_sample is not None:
                    on_sample(path)

            for sample_id in ids:
                for name in collections:
                    exists = conn.execute(
                        select(t.sample_collection.c.sample_id).where(
                            and_(
                                t.sample_collection.c.sample_id == sample_id,
                                t.sample_collection.c.collection == name,
                            )
                        )
                    ).first()
                    if not exists:
                        conn.execute(
                            insert(t.sample_collection).values(
                                sample_id=sample_id, collection=name
                            )
                        )

            # Inside the transaction and before it commits: a backend that
            # packs has not made its objects exist yet, and rows naming a
            # shard that failed to upload would be worse than no rows.
            self.blobs.flush()
        return ids

    # ------------------------------------------------------------------
    # Label sets
    # ------------------------------------------------------------------

    def create_label_set(self, name: str, schema: AnySchema) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                insert(t.label_set).values(name=name, schema=json.loads(schema.model_dump_json()))
            ).inserted_primary_key[0]

    def label_set(self, name: str) -> tuple[int, AnySchema]:
        """A label set by name, as whatever kind of schema it is.

        Read back through the discriminator rather than as one task's
        schema: a label set is how a corpus is annotated, and pinning it to
        classification would mean a catalog could hold boxes it could never
        hand back.
        """
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.label_set.c.id, t.label_set.c.schema).where(t.label_set.c.name == name)
            ).first()
        if row is None:
            raise CatalogError(f"No label set named {name!r}")
        return row.id, _SCHEMA.validate_python(row.schema)

    def set_classes(self, label_set_id: int, schema: AnySchema) -> None:
        """Replace a label set's schema.

        Append-only is a convention rather than a constraint here, because a
        run records the class list it trained with — that, not this table, is
        what a checkpoint is checked against.
        """
        with self.engine.begin() as conn:
            conn.execute(
                update(t.label_set)
                .where(t.label_set.c.id == label_set_id)
                .values(schema=json.loads(schema.model_dump_json()))
            )

    # ------------------------------------------------------------------
    # Annotations
    # ------------------------------------------------------------------

    def annotate(
        self,
        sample_id: int,
        label_set_id: int,
        value: AnyValue,
        source: str = "human",
    ) -> None:
        """Record what a sample is, and index the classes it asserts."""
        _, schema = self._label_set_by_id(label_set_id)
        schema.validate_value(value)
        with self.engine.begin() as conn:
            self._upsert_annotation(
                conn,
                sample_id,
                label_set_id,
                state=t.ANNOTATED,
                value=json.loads(value.model_dump_json()),
                source=source,
            )
            # Someone has looked again, which is what a conflict was asking
            # for. Whichever way they went, it is settled.
            self._clear_conflict(conn, sample_id, label_set_id)
            self._reindex_classes(
                conn, sample_id, label_set_id, schema.classes_asserted(value)
            )

    def skip(self, sample_id: int, label_set_id: int) -> None:
        """Mark a sample reviewed with nothing applicable.

        Excluded from datasets and from the review queue alike, so it does
        not come back round.
        """
        with self.engine.begin() as conn:
            self._upsert_annotation(
                conn, sample_id, label_set_id, state=t.SKIPPED, value=None, source="human"
            )
            self._reindex_classes(conn, sample_id, label_set_id, set())

    def annotate_many(
        self,
        label_set_id: int,
        items: Iterable[tuple[int, AnyValue | None]],
        source: str = "human",
        on_item: Callable[[int], None] | None = None,
    ) -> tuple[int, int]:
        """Record many annotations in one transaction; return (annotated, skipped).

        A ``None`` value means skipped, mirroring the column: there is no
        answer, as against an empty value, which is the answer "nothing
        here". Bulk because a commit per annotation costs an fsync, and the
        schema is fetched once rather than per row.
        """
        _, schema = self._label_set_by_id(label_set_id)
        annotated = skipped = 0
        with self.engine.begin() as conn:
            for sample_id, value in items:
                if value is None:
                    self._upsert_annotation(
                        conn, sample_id, label_set_id, state=t.SKIPPED, value=None, source=source
                    )
                    self._reindex_classes(conn, sample_id, label_set_id, set())
                    skipped += 1
                else:
                    schema.validate_value(value)
                    self._upsert_annotation(
                        conn,
                        sample_id,
                        label_set_id,
                        state=t.ANNOTATED,
                        value=json.loads(value.model_dump_json()),
                        source=source,
                    )
                    self._reindex_classes(
                        conn, sample_id, label_set_id, schema.classes_asserted(value)
                    )
                    annotated += 1
                if on_item is not None:
                    on_item(sample_id)
        return annotated, skipped

    def unskip(self, label_set_id: int, sample_ids: Iterable[int]) -> int:
        """Return skipped samples to the queue; returns how many moved.

        The row is deleted rather than flagged, because the queue is defined
        by the absence of one. Anything annotated is left alone: a skip is
        the only state this undoes, and quietly discarding an answer would
        be a far worse thing to do by accident.
        """
        moved = 0
        with self.engine.begin() as conn:
            for sample_id in sample_ids:
                where = and_(
                    t.annotation.c.sample_id == sample_id,
                    t.annotation.c.label_set_id == label_set_id,
                    t.annotation.c.state == t.SKIPPED,
                )
                if conn.execute(delete(t.annotation).where(where)).rowcount:
                    self._reindex_classes(conn, sample_id, label_set_id, set())
                    moved += 1
        return moved

    def discard(self, label_set_id: int, source: str) -> int:
        """Delete annotations from one source; returns how many went.

        For rows that were never really answers. A batch imported from
        somewhere else is a set of candidates: useful to review against,
        and not something to train on until somebody has confirmed it.
        Left in place it is indistinguishable from an answer, and a
        document annotated only in part is worse than one not annotated at
        all — every class nobody marked reads as a deliberate negative.

        The row is deleted rather than flagged, for the reason ``unskip``
        deletes: unlabelled is the absence of a row, and that is exactly
        the state an unreviewed import should be returned to.

        Scoped by source, and the source has to be named, so an answer a
        person actually gave is never removed by a call that meant
        something else.
        """
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(t.annotation.c.sample_id).where(
                    and_(
                        t.annotation.c.label_set_id == label_set_id,
                        t.annotation.c.source == source,
                    )
                )
            ).all()
            for row in rows:
                conn.execute(
                    delete(t.annotation).where(
                        and_(
                            t.annotation.c.sample_id == row.sample_id,
                            t.annotation.c.label_set_id == label_set_id,
                            t.annotation.c.source == source,
                        )
                    )
                )
                self._reindex_classes(conn, row.sample_id, label_set_id, set())
        return len(rows)

    def _upsert_annotation(self, conn, sample_id, label_set_id, *, state, value, source):
        where = and_(
            t.annotation.c.sample_id == sample_id,
            t.annotation.c.label_set_id == label_set_id,
        )
        exists = conn.execute(select(t.annotation.c.sample_id).where(where)).first()
        if exists:
            conn.execute(
                update(t.annotation).where(where).values(state=state, value=value, source=source)
            )
        else:
            conn.execute(
                insert(t.annotation).values(
                    sample_id=sample_id,
                    label_set_id=label_set_id,
                    state=state,
                    value=value,
                    source=source,
                )
            )

    def _reindex_classes(self, conn, sample_id, label_set_id, classes: set[str]) -> None:
        conn.execute(
            delete(t.annotation_class).where(
                and_(
                    t.annotation_class.c.sample_id == sample_id,
                    t.annotation_class.c.label_set_id == label_set_id,
                )
            )
        )
        for name in sorted(classes):
            conn.execute(
                insert(t.annotation_class).values(
                    sample_id=sample_id, label_set_id=label_set_id, class_name=name
                )
            )

    def _label_set_by_id(self, label_set_id: int) -> tuple[int, AnySchema]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.label_set.c.schema).where(t.label_set.c.id == label_set_id)
            ).first()
        if row is None:
            raise CatalogError(f"No label set with id {label_set_id}")
        return label_set_id, _SCHEMA.validate_python(row.schema)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def _live(self):
        return t.sample.c.deleted_at.is_(None)

    def _scoped(self, stmt, collections):
        """Restrict a sample query to the collections a caller named.

        An EXISTS rather than a join, because a sample in three collections
        would otherwise come back three times and need a DISTINCT to fix —
        and Postgres cannot take DISTINCT over a json column at all, so the
        obvious shape fails on one dialect and silently duplicates on the
        other.
        """
        within = _within(collections)
        if within is None:
            return stmt
        return stmt.where(
            select(t.sample_collection.c.sample_id)
            .where(
                and_(t.sample_collection.c.sample_id == t.sample.c.id, within)
            )
            .exists()
        )

    def _rows(self, conn, stmt) -> list[SampleRow]:
        return [
            SampleRow(
                id=r.id,
                checksum=r.checksum,
                location=Location(r.location, r.offset, r.length),
                media=r.media,
                subtype=r.subtype,
                group_id=r.group_id,
                metadata=r.metadata,
            )
            for r in conn.execute(stmt)
        ]

    _COLUMNS = (
        t.sample.c.id,
        t.sample.c.checksum,
        t.sample.c.location,
        t.sample.c.offset,
        t.sample.c.length,
        t.sample.c.media,
        t.sample.c.subtype,
        t.sample.c.group_id,
        t.sample.c.metadata,
    )

    def unlabelled(
        self, label_set_id: int, collections, limit: int | None = None
    ) -> list[SampleRow]:
        """Samples nobody has dealt with, drawn from ``collections``.

        Per label set, not global: a sample can be classified and still be
        waiting for boxes. Skipped samples have a row, so they are excluded
        by the same join rather than by a second condition.

        Scoped, because a catalog holding several jobs' data would otherwise
        offer every one of them to every job. What a project draws from is
        the project's declaration, not the catalog's.
        """
        stmt = (
            select(*self._COLUMNS)
            .outerjoin(
                t.annotation,
                and_(
                    t.annotation.c.sample_id == t.sample.c.id,
                    t.annotation.c.label_set_id == label_set_id,
                ),
            )
            .where(and_(self._live(), t.annotation.c.sample_id.is_(None)))
        )
        stmt = self._scoped(stmt, collections)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self.engine.connect() as conn:
            return self._rows(conn, stmt)

    def labelled(self, label_set_id: int, collections) -> list[SampleRow]:
        """Samples with a real answer — skipped ones are not training data.

        Scoped like the queue. Dropping a collection from a project means
        declaring that data out of scope, training included: quietly
        carrying it would move the metrics as well as the model, and neither
        would say why. Keeping what is already answered while asking for no
        more is what skipping is for.
        """
        stmt = (
            select(*self._COLUMNS)
            .join(t.annotation, t.annotation.c.sample_id == t.sample.c.id)
            .where(
                and_(
                    self._live(),
                    t.annotation.c.label_set_id == label_set_id,
                    t.annotation.c.state == t.ANNOTATED,
                )
            )
        )
        with self.engine.connect() as conn:
            return self._rows(conn, self._scoped(stmt, collections))

    def skipped(self, label_set_id: int, collections) -> list[SampleRow]:
        """Samples reviewed with nothing applicable.

        Neither training data nor queue: they belong to neither of the other
        two, so anything reconstructing the whole picture needs them named.
        """
        stmt = (
            select(*self._COLUMNS)
            .join(t.annotation, t.annotation.c.sample_id == t.sample.c.id)
            .where(
                and_(
                    self._live(),
                    t.annotation.c.label_set_id == label_set_id,
                    t.annotation.c.state == t.SKIPPED,
                )
            )
        )
        with self.engine.connect() as conn:
            return self._rows(conn, self._scoped(stmt, collections))

    def with_class(self, label_set_id: int, class_name: str, collections) -> list[SampleRow]:
        """Every sample asserting a class — the join the index table exists for."""
        stmt = (
            select(*self._COLUMNS)
            .join(t.annotation_class, t.annotation_class.c.sample_id == t.sample.c.id)
            .where(
                and_(
                    self._live(),
                    t.annotation_class.c.label_set_id == label_set_id,
                    t.annotation_class.c.class_name == class_name,
                )
            )
        )
        with self.engine.connect() as conn:
            return self._rows(conn, self._scoped(stmt, collections))

    def by_checksum(self, checksum: str) -> SampleRow | None:
        """The sample with these bytes.

        How a Label Studio task is recognised on the way back. Keyed on the
        checksum rather than on where the bytes sit, because a location is a
        storage detail that moves — repacking files into shards rewrites
        every one of them — while a task URL created months earlier does
        not. Content addressing is the only identity here that survives the
        blobs being moved.
        """
        stmt = select(*self._COLUMNS).where(
            and_(self._live(), t.sample.c.checksum == checksum)
        )
        with self.engine.connect() as conn:
            rows = self._rows(conn, stmt)
        return rows[0] if rows else None

    def by_location(self, container: str, offset: int = 0) -> SampleRow | None:
        """The sample whose bytes sit at a blob location.

        How a Label Studio task is recognised on the way back: its image URL
        names a location, and a location names one sample.

        Both halves are needed. A container was one file when blobs were
        files, so matching on it alone was unambiguous; a tar shard holds
        hundreds, and matching on it alone returns whichever of them the
        database reaches first. The offset is what tells them apart.
        """
        stmt = select(*self._COLUMNS).where(
            and_(
                self._live(),
                t.sample.c.location == container,
                t.sample.c.offset == offset,
            )
        )
        with self.engine.connect() as conn:
            rows = self._rows(conn, stmt)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Disagreement
    # ------------------------------------------------------------------

    def record_conflict(
        self,
        sample_id: int,
        label_set_id: int,
        kept: AnyValue | None,
        other: AnyValue | None,
        other_origin: str | None = None,
    ) -> None:
        """Note that two origins answered this sample differently.

        Called by whatever merges one catalog into another. Not by
        :meth:`annotate`: a reviewer changing their mind is not a conflict,
        it is the point of being able to correct an answer. A conflict is
        two answers that were made independently, and only a merge can see
        that.

        The catalog keeps the answer it already had. Choosing between them
        is exactly what it cannot do — both were made by someone looking at
        the sample — so it keeps one, remembers the other, and puts the pair
        in front of a person.
        """
        payload = {
            "kept_value": None if kept is None else json.loads(kept.model_dump_json()),
            "other_value": None if other is None else json.loads(other.model_dump_json()),
            "other_origin": other_origin,
        }
        with self.engine.begin() as conn:
            updated = conn.execute(
                update(t.annotation_conflict)
                .where(
                    and_(
                        t.annotation_conflict.c.sample_id == sample_id,
                        t.annotation_conflict.c.label_set_id == label_set_id,
                    )
                )
                .values(**payload)
            ).rowcount
            if not updated:
                conn.execute(
                    insert(t.annotation_conflict).values(
                        sample_id=sample_id, label_set_id=label_set_id, **payload
                    )
                )

    def conflicts(self, label_set_id: int, collections) -> list[dict]:
        """Samples whose answer is disputed, and what the two answers were."""
        stmt = (
            select(
                t.sample.c.id,
                t.sample.c.checksum,
                t.annotation_conflict.c.kept_value,
                t.annotation_conflict.c.other_value,
                t.annotation_conflict.c.other_origin,
            )
            .select_from(
                t.annotation_conflict.join(
                    t.sample, t.sample.c.id == t.annotation_conflict.c.sample_id
                )
            )
            .where(
                and_(
                    self._live(),
                    t.annotation_conflict.c.label_set_id == label_set_id,
                )
            )
        )
        def value(raw):
            return None if raw is None else _VALUE.validate_python(raw)

        with self.engine.connect() as conn:
            return [
                {
                    "sample_id": row.id,
                    "checksum": row.checksum,
                    "kept": value(row.kept_value),
                    "other": value(row.other_value),
                    "origin": row.other_origin,
                }
                for row in conn.execute(self._scoped(stmt, collections))
            ]

    def _clear_conflict(self, conn, sample_id: int, label_set_id: int) -> None:
        """A fresh answer settles it, whichever way it went."""
        conn.execute(
            delete(t.annotation_conflict).where(
                and_(
                    t.annotation_conflict.c.sample_id == sample_id,
                    t.annotation_conflict.c.label_set_id == label_set_id,
                )
            )
        )

    def annotation_of(self, sample_id: int, label_set_id: int) -> AnyValue | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.annotation.c.state, t.annotation.c.value).where(
                    and_(
                        t.annotation.c.sample_id == sample_id,
                        t.annotation.c.label_set_id == label_set_id,
                    )
                )
            ).first()
        if row is None or row.state == t.SKIPPED:
            return None
        # Through the discriminator: read back as one task's value, a boxes
        # annotation parses without complaint into an empty Choices, and the
        # catalog silently forgets what a human actually said.
        return _VALUE.validate_python(row.value)

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------

    def create_dataset(
        self,
        name: str,
        label_set_id: int,
        sample_ids: Sequence[int] | None = None,
        collections=None,
        val_ratio: float = 0.2,
        seed: int = 42,
        query: dict | None = None,
    ) -> int:
        """Freeze a selection into a new version, inheriting the previous split.

        Membership is written down rather than derived, and every sample the
        previous version already placed keeps its side. Only what is new gets
        decided, so the split cannot drift as the labelled set grows.
        """
        if sample_ids is None:
            if collections is None:
                raise CatalogError(
                    "Give either the samples to freeze or the collections to "
                    "draw them from; defaulting to the whole catalog would "
                    "quietly train on another job's data."
                )
            sample_ids = [s.id for s in self.labelled(label_set_id, collections)]
        if not sample_ids:
            raise CatalogError(f"No labelled samples for label set {label_set_id}")

        with self.engine.begin() as conn:
            existing = self._identical_version(conn, name, set(sample_ids))
            if existing is not None:
                # A version describes a selection, not an attempt at one. A
                # round that crashed after freezing its dataset should be
                # retried against the same version rather than minting a
                # second one that says exactly the same thing.
                return existing
            version = (
                conn.execute(
                    select(t.dataset.c.version)
                    .where(t.dataset.c.name == name)
                    .order_by(t.dataset.c.version.desc())
                    .limit(1)
                ).scalar_one_or_none()
                or 0
            ) + 1
            inherited = self._previous_split(conn, name)
            # Chunked for the reason _chunks exists: one bound parameter per
            # sample, against a limit that depends on the interpreter. An
            # apt-installed Python allows 250,000 of them and a uv-managed
            # one 32,766, so a corpus this held fine on one machine failed on
            # another — and failed before the round did anything, which at
            # least made it loud.
            groups: dict[int, str | None] = {}
            for chunk in _chunks(list(sample_ids), 500):
                groups.update(
                    conn.execute(
                        select(t.sample.c.id, t.sample.c.group_id).where(
                            t.sample.c.id.in_(chunk)
                        )
                    ).all()
                )
            flags, achieved = assign(groups, inherited, val_ratio=val_ratio, seed=seed)

            dataset_id = conn.execute(
                insert(t.dataset).values(
                    name=name,
                    version=version,
                    label_set_id=label_set_id,
                    query=query,
                    val_ratio=val_ratio,
                    val_ratio_achieved=achieved,
                )
            ).inserted_primary_key[0]
            # One statement per chunk rather than per sample: freezing a
            # version of a large corpus was 54,000 round trips, all inside
            # the same transaction and all doing the same thing.
            members = [
                {"dataset_id": dataset_id, "sample_id": sample_id, "val": is_val}
                for sample_id, is_val in flags.items()
            ]
            for chunk in _chunks(members, 500):
                conn.execute(insert(t.dataset_member), chunk)
        return dataset_id

    def _identical_version(self, conn, name: str, wanted: set[int]) -> int | None:
        """The latest version of ``name``, if it holds exactly ``wanted``."""
        latest = conn.execute(
            select(t.dataset.c.id)
            .where(t.dataset.c.name == name)
            .order_by(t.dataset.c.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest is None:
            return None
        members = {
            row[0]
            for row in conn.execute(
                select(t.dataset_member.c.sample_id).where(
                    t.dataset_member.c.dataset_id == latest
                )
            )
        }
        return latest if members == wanted else None

    def _previous_split(self, conn, name: str) -> dict[int, bool]:
        previous = conn.execute(
            select(t.dataset.c.id)
            .where(t.dataset.c.name == name)
            .order_by(t.dataset.c.version.desc())
            .limit(1)
        ).scalar_one_or_none()
        if previous is None:
            return {}
        return dict(
            conn.execute(
                select(t.dataset_member.c.sample_id, t.dataset_member.c.val).where(
                    t.dataset_member.c.dataset_id == previous
                )
            ).all()
        )

    # ------------------------------------------------------------------
    # Materialise
    # ------------------------------------------------------------------

    def ensure_cached(
        self, checksums: Sequence[str], cache: Path, on_progress=None
    ) -> dict[str, Path]:
        """Files on this host for these samples, fetching what is missing.

        For work that is not a dataset — ranking a review pool means scoring
        every unlabelled sample, and those are by definition in no dataset
        version. The cache is content-addressed, so a sample already pulled
        for a dataset is already here, and a sample pulled for this is there
        for the next one.

        Returns only what the catalog knows. A checksum it has never seen is
        absent rather than an error, because the caller asked about samples
        and is entitled to hear that one is not among them.
        """
        cache = Path(cache)
        paths: dict[str, Path] = {}
        missing: list[tuple[Location, Path]] = []

        with self.engine.connect() as conn:
            for chunk in _chunks(list(checksums), 500):
                rows = conn.execute(
                    select(
                        t.sample.c.checksum,
                        t.sample.c.location,
                        t.sample.c.offset,
                        t.sample.c.length,
                        t.sample.c.metadata,
                    ).where(and_(self._live(), t.sample.c.checksum.in_(chunk)))
                )
                for row in rows:
                    suffix = Path(
                        (row.metadata or {}).get("source_path") or ""
                    ).suffix.lower()
                    target = cache / blob_path(row.checksum, suffix)
                    paths[row.checksum] = target
                    if not target.exists():
                        missing.append(
                            (Location(row.location, row.offset, row.length), target)
                        )

        done, total = len(paths) - len(missing), len(paths)
        if on_progress is not None:
            on_progress(done, total)
        if not missing:
            return paths

        for _, target in missing:
            target.parent.mkdir(parents=True, exist_ok=True)

        wanted = dict(missing)
        path_for = getattr(self.blobs, "path_for", None)
        if path_for is None:
            for location, body in self.blobs.fetch(list(wanted)):
                target = wanted[location]
                # Through a temporary name: a short file at the address of a
                # whole one is served as a hit forever, and nothing rehashes
                # a cache entry to notice.
                partial = target.with_name(target.name + ".partial")
                partial.write_bytes(body)
                partial.replace(target)
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
        else:
            for location, target in wanted.items():
                _link_or_copy(path_for(location), target)
                done += 1
                if on_progress is not None:
                    on_progress(done, total)
        return paths

    def composition(self, collections) -> dict[tuple[str, str], int]:
        """What the samples in these collections are, keyed (media, subtype).

        A project declares both so ``ingest`` can work before anything is
        catalogued: media picks which files count, and subtype decides
        whether they are grouped. Afterwards the samples are the truth, and
        the two can disagree — a collection a project did not fill, or a
        declaration changed after the fact.

        Subtype is the one that matters quietly. Frames ingested as plain
        images each become their own group, so near-duplicates land on both
        sides of a train/val split and validation reads high for a model
        that has memorised them.
        """
        stmt = (
            select(t.sample.c.media, t.sample.c.subtype, func.count())
            .where(self._live())
            .group_by(t.sample.c.media, t.sample.c.subtype)
        )
        with self.engine.connect() as conn:
            return {
                (row[0], row[1]): row[2]
                for row in conn.execute(self._scoped(stmt, collections))
            }

    def dataset_named(self, dataset_id: int) -> tuple[str, int]:
        """A dataset's name and version, without materialising it.

        So a caller can work out where a version would live, and whether it
        already holds it, before paying to fetch it. Reading either off the
        manifest is only possible once the files are written, which is too
        late to decide not to write them.
        """
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.dataset.c.name, t.dataset.c.version).where(
                    t.dataset.c.id == dataset_id
                )
            ).first()
        if row is None:
            raise CatalogError(f"No dataset with id {dataset_id}")
        return row.name, row.version

    def dataset_version(self, dataset_id: int) -> int:
        """Which version a dataset id is. See :meth:`dataset_named`."""
        return self.dataset_named(dataset_id)[1]

    def materialise(
        self, dataset_id: int, dest: Path, on_progress=None, cache: Path | None = None
    ) -> Path:
        """Write a dataset version out as files plus a manifest.

        The result needs no database and no catalog to train from, which is
        what makes a dataset version the portable unit. Files are addressed
        by checksum, so re-materialising after a labelling round rewrites the
        manifest and copies nothing.

        ``on_progress(done, total)`` is called as blobs land. It exists
        because this stopped being instant: linking local files is over
        before anyone looks, but pulling shards out of a bucket is minutes
        of silence, and silence is indistinguishable from a hang.

        ``cache`` is a directory of blobs by checksum, consulted before the
        backend and filled from it. Successive versions of a dataset share
        almost all their samples, so without one every version re-fetches a
        corpus it already has on disk. A hit costs a hard link.
        """
        dest = Path(dest)
        files = dest / FILES_DIR
        files.mkdir(parents=True, exist_ok=True)

        with self.engine.connect() as conn:
            info = conn.execute(
                select(
                    t.dataset.c.name,
                    t.dataset.c.version,
                    t.dataset.c.label_set_id,
                    t.dataset.c.val_ratio,
                    t.dataset.c.val_ratio_achieved,
                    t.label_set.c.name.label("label_set"),
                    t.label_set.c.schema,
                )
                .join(t.label_set, t.label_set.c.id == t.dataset.c.label_set_id)
                .where(t.dataset.c.id == dataset_id)
            ).first()
            if info is None:
                raise CatalogError(f"No dataset with id {dataset_id}")
            # The label set is resolved above rather than joined in here: an
            # ON clause cannot reference a table joined after it, and binding
            # the id drops a three-way join to a two-way one.
            rows = conn.execute(
                select(
                    t.sample.c.id,
                    t.sample.c.checksum,
                    t.sample.c.location,
                    t.sample.c.offset,
                    t.sample.c.length,
                    t.sample.c.group_id,
                    t.sample.c.metadata,
                    t.dataset_member.c.val,
                    t.annotation.c.state,
                    t.annotation.c.value,
                )
                .join(t.dataset_member, t.dataset_member.c.sample_id == t.sample.c.id)
                .outerjoin(
                    t.annotation,
                    and_(
                        t.annotation.c.sample_id == t.sample.c.id,
                        t.annotation.c.label_set_id == info.label_set_id,
                    ),
                )
                .where(t.dataset_member.c.dataset_id == dataset_id)
            ).all()

        wanted: dict[Location, Path] = {}
        relatives: dict[int, str] = {}
        for row in rows:
            relatives[row.id] = f"{FILES_DIR}/{_materialised_name(row)}"
            target = dest / relatives[row.id]
            if not target.exists():
                wanted[Location(row.location, row.offset, row.length)] = target
        self._write_out(wanted, on_progress, cache)

        samples = []
        for row in rows:
            relative = relatives[row.id]
            samples.append(
                ManifestSample(
                    id=row.id,
                    checksum=row.checksum,
                    path=relative,
                    group_id=row.group_id,
                    val=bool(row.val),
                    value=(
                        _VALUE.validate_python(row.value)
                        if row.state == t.ANNOTATED and row.value is not None
                        else None
                    ),
                )
            )

        manifest = Manifest(
            dataset=info.name,
            version=info.version,
            catalog_id=self.id,
            label_set=info.label_set,
            label_schema=_SCHEMA.validate_python(info.schema),
            val_ratio=info.val_ratio,
            val_ratio_achieved=info.val_ratio_achieved,
            samples=samples,
        )
        (dest / MANIFEST_NAME).write_text(manifest.model_dump_json(indent=2))
        return dest

    def _write_out(
        self, wanted: dict[Location, Path], on_progress=None, cache: Path | None = None
    ) -> None:
        """Put every wanted blob where the manifest says it is.

        Split by what the backend can do rather than done uniformly. A
        backend with files behind it links them, so a dataset version costs
        no disk. One with blobs packed in a bucket is asked for them
        together, so a shard is pulled once instead of range-requested per
        member — which is the difference between one object and a thousand
        requests for a dataset that lives in one shard.
        """
        if not wanted:
            return
        for target in wanted.values():
            target.parent.mkdir(parents=True, exist_ok=True)

        done = 0
        total = len(wanted)

        def tick() -> None:
            nonlocal done
            done += 1
            if on_progress is not None:
                on_progress(done, total)

        # The cache first, because a hit costs a link and a miss costs a
        # network round trip. A materialised file is named for its checksum,
        # so where it would live in the cache is derivable from where it is
        # going — no second lookup, and no need to carry checksums here.
        remaining = wanted
        if cache is not None:
            cache = Path(cache)
            remaining = {}
            for location, target in wanted.items():
                candidate = cache / blob_path(target.stem, target.suffix)
                if candidate.exists():
                    _link_or_copy(candidate, target)
                    tick()
                else:
                    remaining[location] = target

        if not remaining:
            # Everything came from the cache. Asking a backend for nothing
            # is a round trip that can only fail.
            return

        path_for = getattr(self.blobs, "path_for", None)
        if path_for is None:
            for location, body in self.blobs.fetch(list(remaining)):
                target = remaining[location]
                if cache is None:
                    target.write_bytes(body)
                else:
                    # Written to the cache and linked from it, so the bytes
                    # exist once however many versions reference them. Via a
                    # temporary name: an interrupted write must not leave a
                    # short file at the address of a whole one, which would
                    # then be served as a cache hit forever.
                    cached = cache / blob_path(target.stem, target.suffix)
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    partial = cached.with_name(cached.name + ".partial")
                    partial.write_bytes(body)
                    partial.replace(cached)
                    _link_or_copy(cached, target)
                tick()
            return

        for location, target in remaining.items():
            _link_or_copy(path_for(location), target)
            tick()
