"""Samples: the rows behind the bytes, and every query that draws on them.

Reads join whatever they need; writes touch the sample and collection
tables only. Everything here is scoped to the collections a caller names
(:func:`rows.scoped`), and nothing here reads a file: bytes are the blob
backend's, and canonical form is a sample type's.
"""

from collections.abc import Iterable, Sequence

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.engine import Engine

from ..rows import SAMPLE_COLUMNS, SampleRow, chunks, current, live, sample_rows, scoped
from ..storage.blobs import Location
from . import tables as t


class Samples:
    """The catalog's samples."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # -- within a caller's transaction ----------------------------------

    def find(self, conn, checksum: str) -> int | None:
        """The id for these bytes, if they are catalogued."""
        return conn.execute(
            select(t.sample.c.id).where(t.sample.c.checksum == checksum)
        ).scalar_one_or_none()

    def add(
        self,
        conn,
        checksum: str,
        location: Location,
        *,
        media: str,
        subtype: str,
        metadata: dict | None,
    ) -> int:
        """A new row for bytes already stored at ``location``."""
        return conn.execute(
            insert(t.sample).values(
                location=location.container,
                offset=location.offset,
                length=location.length,
                checksum=checksum,
                media=media,
                subtype=subtype,
                metadata=metadata,
            )
        ).inserted_primary_key[0]

    def describe(self, conn, sample_id: int, *, subtype: str, metadata: dict | None) -> None:
        """Bring a known sample's description up to date.

        Updated rather than left alone, so correcting how a corpus is
        described is one re-run rather than a rebuild. Regrouping cannot
        disturb a dataset already built: membership is materialised into
        ``dataset_member``, so only later versions see the change.
        """
        conn.execute(
            update(t.sample)
            .where(t.sample.c.id == sample_id)
            .values(subtype=subtype, metadata=metadata)
        )

    def collect(self, conn, sample_ids: Iterable[int], collections: Iterable[str]) -> None:
        """Add samples to collections.

        Added rather than replaced: a sample already in one collection that
        turns up in another belongs to both, which is what makes a corpus
        reusable across jobs rather than owned by the first.
        """
        for sample_id in sample_ids:
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
                        insert(t.sample_collection).values(sample_id=sample_id, collection=name)
                    )

    def grouping(self, conn, sample_ids: Sequence[int], key: str | None) -> dict[int, str | None]:
        """Each sample's group under metadata ``key``, None for its own.

        No key means no grouping: every sample is its own group. A sample
        without the key, or with a null under it, is its own group too —
        nothing groups unless the data says so.
        """
        if key is None:
            return dict.fromkeys(sample_ids)
        return {
            sample_id: (None if (value := metadata.get(key)) is None else str(value))
            for sample_id, metadata in self.metadata(conn, sample_ids).items()
        }

    def metadata(self, conn, sample_ids: Sequence[int]) -> dict[int, dict]:
        """Each sample's recorded metadata, empty where there is none."""
        found: dict[int, dict] = {}
        for chunk in chunks(list(sample_ids)):
            rows = conn.execute(
                select(t.sample.c.id, t.sample.c.metadata).where(t.sample.c.id.in_(chunk))
            ).all()
            found.update({sample_id: metadata or {} for sample_id, metadata in rows})
        return found

    def located(self, conn, checksums: Sequence[str]) -> list:
        """Checksum, location and metadata for the live samples among ``checksums``."""
        rows = []
        for chunk in chunks(list(checksums)):
            rows.extend(
                conn.execute(
                    select(
                        t.sample.c.checksum,
                        t.sample.c.location,
                        t.sample.c.offset,
                        t.sample.c.length,
                        t.sample.c.metadata,
                    ).where(and_(live(), t.sample.c.checksum.in_(chunk)))
                ).all()
            )
        return rows

    # -- queries -----------------------------------------------------------

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
            select(*SAMPLE_COLUMNS)
            .outerjoin(
                t.annotation,
                and_(
                    t.annotation.c.sample_id == t.sample.c.id,
                    t.annotation.c.label_set_id == label_set_id,
                    current(),
                ),
            )
            .where(and_(live(), t.annotation.c.sample_id.is_(None)))
        )
        stmt = scoped(stmt, collections)
        if limit is not None:
            stmt = stmt.limit(limit)
        with self.engine.connect() as conn:
            return sample_rows(conn, stmt)

    def labelled(self, label_set_id: int, collections) -> list[SampleRow]:
        """Samples with a real answer — skipped ones are not training data.

        Scoped like the queue. Dropping a collection from a project means
        declaring that data out of scope, training included: quietly
        carrying it would move the metrics as well as the model, and neither
        would say why. Keeping what is already answered while asking for no
        more is what skipping is for.
        """
        return self._joined(
            t.annotation,
            t.annotation.c.label_set_id == label_set_id,
            t.annotation.c.state == t.ANNOTATED,
            current(),
            collections=collections,
        )

    def unreviewed(self, label_set_id: int, collections) -> list[SampleRow]:
        """Samples whose current answer arrived with the corpus and nobody has confirmed.

        Trusted and trained on, since an import is an answer; listed so a
        spot review can pick among them, by wherever a model disagrees with
        what was imported.
        """
        return self._joined(
            t.annotation,
            t.annotation.c.label_set_id == label_set_id,
            t.annotation.c.state == t.ANNOTATED,
            t.annotation.c.source == t.IMPORT,
            current(),
            collections=collections,
        )

    def skipped(self, label_set_id: int, collections) -> list[SampleRow]:
        """Samples reviewed with nothing applicable.

        Neither training data nor queue: they belong to neither of the other
        two, so anything reconstructing the whole picture needs them named.
        """
        return self._joined(
            t.annotation,
            t.annotation.c.label_set_id == label_set_id,
            t.annotation.c.state == t.SKIPPED,
            current(),
            collections=collections,
        )

    def with_class(self, label_set_id: int, class_name: str, collections) -> list[SampleRow]:
        """Every sample asserting a class — the join the index table exists for."""
        return self._joined(
            t.annotation_class,
            t.annotation_class.c.label_set_id == label_set_id,
            t.annotation_class.c.class_name == class_name,
            collections=collections,
        )

    def _joined(self, table, *predicates, collections) -> list[SampleRow]:
        """Live samples with a row in ``table`` meeting ``predicates``, in scope."""
        stmt = (
            select(*SAMPLE_COLUMNS)
            .join(table, table.c.sample_id == t.sample.c.id)
            .where(and_(live(), *predicates))
        )
        with self.engine.connect() as conn:
            return sample_rows(conn, scoped(stmt, collections))

    def by_checksum(self, checksum: str) -> SampleRow | None:
        """The sample with these bytes, or None.

        The lookup behind every reference that has to survive a move: a
        task URL, a cache key, a manifest entry. See ``docs/adr/0001``.
        """
        stmt = select(*SAMPLE_COLUMNS).where(
            and_(live(), t.sample.c.checksum == checksum)
        )
        with self.engine.connect() as conn:
            rows = sample_rows(conn, stmt)
        return rows[0] if rows else None

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
            .where(live())
            .group_by(t.sample.c.media, t.sample.c.subtype)
        )
        with self.engine.connect() as conn:
            return {
                (row[0], row[1]): row[2]
                for row in conn.execute(scoped(stmt, collections))
            }
