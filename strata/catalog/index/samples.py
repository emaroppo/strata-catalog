"""Samples: the rows behind the bytes, and every query that draws on them.

Reads join whatever they need; writes touch the sample and collection
tables only. Everything here is scoped to the collections a caller names
(:func:`rows.scoped`), and nothing here reads a file: bytes are the blob
backend's, and canonical form is a sample type's.
"""

from collections.abc import Iterable, Sequence

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.engine import Engine

from ..rows import (
    SAMPLE_COLUMNS,
    AmbiguousSample,
    SampleRow,
    chunks,
    current,
    live,
    sample_rows,
    scoped,
    split_type,
)
from ..selection import Compare, compares
from ..storage.blobs import Location
from . import tables as t
from .locks import claim


class Samples:
    """The catalog's samples."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # -- within a caller's transaction ----------------------------------

    def find(self, conn, checksum: str, *, media: str, subtype: str) -> int | None:
        """The id for these bytes as this type, if they are catalogued so.

        The same bytes as another type are another sample. See
        ``docs/adr/0052``.
        """
        return conn.execute(
            select(t.sample.c.id).where(
                t.sample.c.checksum == checksum,
                t.sample.c.media == media,
                t.sample.c.subtype == subtype,
            )
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

    def describe(self, conn, sample_id: int, *, metadata: dict | None) -> None:
        """Bring a known sample's metadata up to date.

        Updated rather than left alone; a dataset already built is
        undisturbed, since membership is materialised into
        ``dataset_member``. See ``docs/adr/0023``. Never its type: with the
        checksum, the type is which sample this is, and the same bytes as
        another type are another sample. See ``docs/adr/0052``. A change in
        place, so what it replaced is an event, for a freeze's replay to
        read back. Nothing changed is nothing recorded. See
        ``docs/adr/0050``.
        """
        before = conn.execute(
            select(t.sample.c.subtype, t.sample.c.metadata).where(t.sample.c.id == sample_id)
        ).one()
        if metadata == before.metadata:
            return
        # The sample, then the event: the order a freeze locks them in
        conn.execute(update(t.sample).where(t.sample.c.id == sample_id).values(metadata=metadata))
        conn.execute(
            insert(t.event).values(
                kind=t.DESCRIBED,
                sample_id=sample_id,
                # The subtype as well, unchanged, as events written while
                # describing could retype a sample carry it
                before={"subtype": before.subtype, "metadata": before.metadata},
            )
        )

    def collect(self, conn, sample_ids: Iterable[int], collections: Iterable[str]) -> None:
        """Add samples to collections.

        Added rather than replaced: a sample already in one collection that
        turns up in another belongs to both. See ``docs/adr/0022``.
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
        without the key, or with a null under it, is its own group too. See
        ``docs/adr/0023``.
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

    def checksums(self, conn, sample_ids: Sequence[int]) -> dict[int, str]:
        """Each sample's checksum, by id."""
        found: dict[int, str] = {}
        for chunk in chunks(list(sample_ids)):
            rows = conn.execute(
                select(t.sample.c.id, t.sample.c.checksum).where(t.sample.c.id.in_(chunk))
            ).all()
            found.update(dict(rows))
        return found

    def identities(self, conn, sample_ids: Sequence[int]) -> dict[int, tuple[str, str]]:
        """Each sample's checksum and type, ``text/email``, by id. See ``docs/adr/0052``."""
        found: dict[int, tuple[str, str]] = {}
        for chunk in chunks(list(sample_ids)):
            for row in conn.execute(
                select(
                    t.sample.c.id, t.sample.c.checksum, t.sample.c.media, t.sample.c.subtype
                ).where(t.sample.c.id.in_(chunk))
            ):
                found[row.id] = (row.checksum, f"{row.media}/{row.subtype}")
        return found

    def locate(self, checksum: str) -> Location | None:
        """Where the bytes of a live sample with this checksum are, of whatever type.

        One copy per checksum, which every row over it names, so any row
        will do: what reads the bytes alone needs no type. See
        ``docs/adr/0052``.
        """
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.sample.c.location, t.sample.c.offset, t.sample.c.length)
                .where(and_(live(), t.sample.c.checksum == checksum))
                .limit(1)
            ).first()
        return None if row is None else Location(row.location, row.offset, row.length)

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
        by the same join rather than by a second condition. Scoped to the
        collections named. See ``docs/adr/0022``.
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

    def labelled(
        self, label_set_id: int, collections, source: str | None = None
    ) -> list[SampleRow]:
        """Samples with a real answer — skipped ones are not training data.

        Scoped like the queue: dropping a collection from a project declares
        that data out of scope, training included. ``source`` narrows to
        answers from one origin. See ``docs/adr/0022``.
        """
        predicates = [
            t.annotation.c.label_set_id == label_set_id,
            t.annotation.c.state == t.ANNOTATED,
            current(),
        ]
        if source is not None:
            predicates.append(t.annotation.c.source == source)
        return self._joined(t.annotation, *predicates, collections=collections)

    def tombstone(self, sample_ids: Iterable[int]) -> int:
        """Remove samples: a tombstone, not a delete, and their answers go with them.

        Every reader takes live samples only, so a removed sample leaves
        the queue, the labelled set and any version frozen from here on;
        its rows and its history stay. A version already frozen keeps its
        members. Returns how many were live and are not now. Each is an
        event as well. See ``docs/adr/0002`` and ``docs/adr/0050``.
        """
        gone = 0
        with self.engine.begin() as conn:
            claim(conn)
            for chunk in chunks(list(sample_ids)):
                alive = list(
                    conn.execute(
                        select(t.sample.c.id).where(
                            and_(t.sample.c.id.in_(chunk), t.sample.c.deleted_at.is_(None))
                        )
                    ).scalars()
                )
                if not alive:
                    continue
                conn.execute(
                    update(t.sample).where(t.sample.c.id.in_(alive)).values(deleted_at=func.now())
                )
                conn.execute(
                    insert(t.event), [{"kind": t.TOMBSTONED, "sample_id": i} for i in alive]
                )
                gone += len(alive)
        return gone

    def matching(self, collections, where: dict[str, object] | None = None) -> list[SampleRow]:
        """Live samples in ``collections`` whose metadata has every key of ``where`` at its value.

        Compared with their types, as a selection spec compares them, by the
        same code: ``2000`` matches the number and not the string. The
        metadata is filtered here rather than in SQL. See ``docs/adr/0021``
        and ``docs/adr/0050``.
        """
        stmt = select(*SAMPLE_COLUMNS).where(live())
        with self.engine.connect() as conn:
            rows = sample_rows(conn, scoped(stmt, collections))
        if not where:
            return rows
        conditions = [
            Compare.model_validate({"key": k, "op": "==", "value": v}) for k, v in where.items()
        ]
        return [row for row in rows if all(compares(c, row.metadata) for c in conditions)]

    def unreviewed(self, label_set_id: int, collections) -> list[SampleRow]:
        """Samples whose current answer arrived with the corpus and nobody has confirmed.

        Trusted and trained on, since an import is an answer; listed so a
        spot review can pick among them. See ``docs/adr/0028``.
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
        """Every sample whose current answer asserts a class: the join the index exists for.

        See ``docs/adr/0039``.
        """
        stmt = (
            select(*SAMPLE_COLUMNS)
            .join(t.annotation, t.annotation.c.sample_id == t.sample.c.id)
            .join(t.annotation_class, t.annotation_class.c.annotation_id == t.annotation.c.id)
            .where(
                and_(
                    live(),
                    t.annotation.c.label_set_id == label_set_id,
                    current(),
                    t.annotation_class.c.class_name == class_name,
                )
            )
        )
        with self.engine.connect() as conn:
            return sample_rows(conn, scoped(stmt, collections))

    def _joined(self, table, *predicates, collections) -> list[SampleRow]:
        """Live samples with a row in ``table`` meeting ``predicates``, in scope."""
        stmt = (
            select(*SAMPLE_COLUMNS)
            .join(table, table.c.sample_id == t.sample.c.id)
            .where(and_(live(), *predicates))
        )
        with self.engine.connect() as conn:
            return sample_rows(conn, scoped(stmt, collections))

    def by_checksum(self, checksum: str, type: str | None = None) -> SampleRow | None:
        """The live sample with these bytes and this type, or None.

        The lookup behind every reference that has to survive a move: a
        task URL, a manifest entry, a list. ``type`` is ``text/email``.
        Without it, the one live sample with these bytes, and
        :class:`AmbiguousSample` if several types hold them: a reference
        written before types were part of one. See ``docs/adr/0001`` and
        ``docs/adr/0052``.
        """
        stmt = select(*SAMPLE_COLUMNS).where(and_(live(), t.sample.c.checksum == checksum))
        if type is not None:
            media, subtype = split_type(type)
            stmt = stmt.where(t.sample.c.media == media, t.sample.c.subtype == subtype)
        with self.engine.connect() as conn:
            rows = sample_rows(conn, stmt)
        if len(rows) > 1:
            raise AmbiguousSample(
                f"{len(rows)} samples hold the bytes {checksum[:12]}: "
                f"{', '.join(sorted(r.type for r in rows))}. Name the type."
            )
        return rows[0] if rows else None

    def composition(self, collections) -> dict[tuple[str, str], int]:
        """What the samples in these collections are, keyed (media, subtype).

        A project declares both so ``ingest`` can work before anything is
        catalogued. Afterwards the samples are the truth, and the two can
        disagree. See ``docs/adr/0010``.
        """
        stmt = (
            select(t.sample.c.media, t.sample.c.subtype, func.count())
            .where(live())
            .group_by(t.sample.c.media, t.sample.c.subtype)
        )
        with self.engine.connect() as conn:
            return {(row[0], row[1]): row[2] for row in conn.execute(scoped(stmt, collections))}
