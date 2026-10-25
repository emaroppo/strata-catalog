"""Samples: the rows behind the bytes, and every query that draws on them, each scoped to
the collections a caller names. See ``docs/adr/0022``.
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
        """The id for these bytes as this type, if they are catalogued so. See ``docs/adr/0052``."""
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
        """Bring a known sample's metadata up to date, never its type; what it replaced is
        an event. See ``docs/adr/0023``, ``docs/adr/0050``, ``docs/adr/0052``.
        """
        before = conn.execute(
            select(t.sample.c.subtype, t.sample.c.metadata).where(t.sample.c.id == sample_id)
        ).one()
        if metadata == before.metadata:
            return
        # The sample, then the event: the order a freeze locks them in. docs/adr/0050
        conn.execute(update(t.sample).where(t.sample.c.id == sample_id).values(metadata=metadata))
        conn.execute(
            insert(t.event).values(
                kind=t.DESCRIBED,
                sample_id=sample_id,
                # The subtype as well, so every DESCRIBED event has one shape
                before={"subtype": before.subtype, "metadata": before.metadata},
            )
        )

    def combine(self, conn, sample_id: int, facts: dict, *, new: bool) -> None:
        """A sample's metadata: its bytes' facts, then each origin's, first value kept; a
        sample not ``new`` is described. See ``docs/adr/0049``, ``docs/adr/0050``.
        """
        combined = dict(facts)
        for (metadata,) in conn.execute(
            select(t.origin.c.metadata)
            .where(t.origin.c.sample_id == sample_id)
            .order_by(t.origin.c.id)
        ):
            for key, value in (metadata or {}).items():
                combined.setdefault(key, value)
        if new:
            conn.execute(
                update(t.sample).where(t.sample.c.id == sample_id).values(metadata=combined or None)
            )
        elif conn.execute(select(t.sample.c.id).where(t.sample.c.id == sample_id, live())).first():
            self.describe(conn, sample_id, metadata=combined or None)

    def collect(self, conn, sample_ids: Iterable[int], collections: Iterable[str]) -> None:
        """Add samples to collections; a sample in one that turns up in another belongs to both.

        See ``docs/adr/0022``.
        """
        names = list(dict.fromkeys(collections))
        if not names:
            return
        for chunk in chunks(list(dict.fromkeys(sample_ids))):
            held = {
                tuple(row)
                for row in conn.execute(
                    select(t.sample_collection.c.sample_id, t.sample_collection.c.collection).where(
                        and_(
                            t.sample_collection.c.sample_id.in_(chunk),
                            t.sample_collection.c.collection.in_(names),
                        )
                    )
                )
            }
            # Sample by sample, so the ids keep that order
            rows = [
                {"sample_id": sample_id, "collection": name}
                for sample_id in chunk
                for name in names
                if (sample_id, name) not in held
            ]
            if rows:
                conn.execute(insert(t.sample_collection), rows)

    def grouping(self, conn, sample_ids: Sequence[int], key: str | None) -> dict[int, str | None]:
        """Each sample's group under metadata ``key``, None for its own; no key, or a null under it,
        is a group of one.

        See ``docs/adr/0023``.
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

        See ``docs/adr/0052``.
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
        """Samples with no current row under ``label_set_id``, drawn from ``collections``; a
        skipped sample has one. See ``docs/adr/0022``.
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
        """Samples with a real answer, skipped ones excluded; ``source`` narrows to one origin.

        See ``docs/adr/0022``.
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
        """Remove samples: a tombstone, not a delete, their answers with them; returns how
        many were live. See ``docs/adr/0002``, ``docs/adr/0050``.
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
        """Live samples in ``collections`` whose metadata has every key of ``where`` at its
        value, compared with their types. See ``docs/adr/0021``, ``docs/adr/0050``.
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

        See ``docs/adr/0028``.
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
        """Samples reviewed with nothing applicable: neither training data nor queue."""
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
        """The live sample with these bytes and this type (``text/email``), or None.

        Without ``type``, the one live sample with these bytes, or :class:`AmbiguousSample`
        if several types hold them. See ``docs/adr/0001``, ``docs/adr/0052``.
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

        See ``docs/adr/0010``.
        """
        stmt = (
            select(t.sample.c.media, t.sample.c.subtype, func.count())
            .where(live())
            .group_by(t.sample.c.media, t.sample.c.subtype)
        )
        with self.engine.connect() as conn:
            return {(row[0], row[1]): row[2] for row in conn.execute(scoped(stmt, collections))}
