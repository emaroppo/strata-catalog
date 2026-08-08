"""The catalog itself: ingest, annotate, query, materialise.

Everything a caller needs goes through here, so the index and the blob
backend stay implementation details. That is what lets SQLite and a
directory be swapped for Postgres and a bucket without a consumer noticing.
"""

import json
import os
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import and_, create_engine, delete, event, insert, or_, select, update
from sqlalchemy.engine import Engine

from strata.labels import Choices, ClassificationSchema

from . import tables as t
from .blobs import BlobBackend, LocalBackend, Location, checksum_of
from .manifest import FILES_DIR, MANIFEST_NAME, Manifest, ManifestSample
from .split import assign


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
    metadata: dict | None = None


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

        The whole batch is one transaction — a commit per file costs an
        fsync each and turns an import into a crawl — so ``on_sample`` is how
        a caller reports progress without breaking that up.
        """
        ids: list[int] = []
        with self.engine.begin() as conn:
            for path in paths:
                path = Path(path)
                extra = metadata_for(path) if metadata_for is not None else None
                entry = {**(metadata or {}), **(extra or {})} or None
                checksum = checksum_of(path)
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
                location = self.blobs.put(path, checksum)
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

    def create_label_set(self, name: str, schema: ClassificationSchema) -> int:
        with self.engine.begin() as conn:
            return conn.execute(
                insert(t.label_set).values(name=name, schema=json.loads(schema.model_dump_json()))
            ).inserted_primary_key[0]

    def label_set(self, name: str) -> tuple[int, ClassificationSchema]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.label_set.c.id, t.label_set.c.schema).where(t.label_set.c.name == name)
            ).first()
        if row is None:
            raise CatalogError(f"No label set named {name!r}")
        return row.id, ClassificationSchema.model_validate(row.schema)

    def set_classes(self, label_set_id: int, schema: ClassificationSchema) -> None:
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
        value: Choices,
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
        items: Iterable[tuple[int, Choices | None]],
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

    def _label_set_by_id(self, label_set_id: int) -> tuple[int, ClassificationSchema]:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.label_set.c.schema).where(t.label_set.c.id == label_set_id)
            ).first()
        if row is None:
            raise CatalogError(f"No label set with id {label_set_id}")
        return label_set_id, ClassificationSchema.model_validate(row.schema)

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

    def annotation_of(self, sample_id: int, label_set_id: int) -> Choices | None:
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
        return Choices.model_validate(row.value)

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
            groups = dict(
                conn.execute(
                    select(t.sample.c.id, t.sample.c.group_id).where(
                        t.sample.c.id.in_(list(sample_ids))
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
            for sample_id, is_val in flags.items():
                conn.execute(
                    insert(t.dataset_member).values(
                        dataset_id=dataset_id, sample_id=sample_id, val=is_val
                    )
                )
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

    def dataset_version(self, dataset_id: int) -> int:
        """Which version a dataset id is, without materialising it.

        So a caller can tell whether it already holds this version before
        paying to fetch it. Reading the version off the manifest is only
        possible once the files are written, which is too late to decide not
        to write them.
        """
        with self.engine.connect() as conn:
            version = conn.execute(
                select(t.dataset.c.version).where(t.dataset.c.id == dataset_id)
            ).scalar()
        if version is None:
            raise CatalogError(f"No dataset with id {dataset_id}")
        return version

    def materialise(self, dataset_id: int, dest: Path) -> Path:
        """Write a dataset version out as files plus a manifest.

        The result needs no database and no catalog to train from, which is
        what makes a dataset version the portable unit. Files are addressed
        by checksum, so re-materialising after a labelling round rewrites the
        manifest and copies nothing.
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
        self._write_out(wanted)

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
                        Choices.model_validate(row.value)
                        if row.state == t.ANNOTATED and row.value is not None
                        else None
                    ),
                )
            )

        manifest = Manifest(
            dataset=info.name,
            version=info.version,
            label_set=info.label_set,
            label_schema=ClassificationSchema.model_validate(info.schema),
            val_ratio=info.val_ratio,
            val_ratio_achieved=info.val_ratio_achieved,
            samples=samples,
        )
        (dest / MANIFEST_NAME).write_text(manifest.model_dump_json(indent=2))
        return dest

    def _write_out(self, wanted: dict[Location, Path]) -> None:
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

        path_for = getattr(self.blobs, "path_for", None)
        if path_for is None:
            for location, body in self.blobs.fetch(list(wanted)):
                wanted[location].write_bytes(body)
            return

        for location, target in wanted.items():
            source = path_for(location)
            try:
                # A blob is immutable and addressed by its content, and a
                # materialised version is derived from it — so the two can
                # share an inode. Without this every dataset version costs a
                # full copy of itself, and a project of any size runs out.
                os.link(source, target)
            except OSError:
                # Different filesystem, or one that will not link.
                # Correctness does not depend on the link, only disk usage.
                shutil.copyfile(source, target)
