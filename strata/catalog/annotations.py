"""Annotations: what was said about a sample, by whom, and disputes over it.

Writes honour :data:`tables.AUTHORITY`: a write never replaces an answer
from a source that outranks it. Unlabelled is the absence of a row, so
returning a sample to the queue deletes one. The class index is rebuilt on
every write through the indexing contract in :mod:`strata.labels`. See
``docs/adr/0009``.
"""

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Engine

from strata.labels import AnySchema, AnyValue

from . import tables as t
from .rows import SCHEMA, VALUE, AnnotateReport, CatalogError, chunks, live, scoped


class Annotations:
    """The catalog's annotations, one row per sample per label set."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def _schema(self, label_set_id: int) -> tuple[int, AnySchema]:
        """The schema a value is validated against. A read of the label set's row."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.label_set.c.schema).where(t.label_set.c.id == label_set_id)
            ).first()
        if row is None:
            raise CatalogError(f"No label set with id {label_set_id}")
        return label_set_id, SCHEMA.validate_python(row.schema)

    def annotate(
        self,
        sample_id: int,
        label_set_id: int,
        value: AnyValue,
        source: str = t.HUMAN,
    ) -> bool:
        """Record what a sample is, and index the classes it asserts.

        Returns whether it was recorded: not when a source outranking this
        one already answered (see :data:`tables.AUTHORITY`).
        """
        _, schema = self._schema(label_set_id)
        schema.validate_value(value)
        with self.engine.begin() as conn:
            written = self._upsert_annotation(
                conn,
                sample_id,
                label_set_id,
                state=t.ANNOTATED,
                value=json.loads(value.model_dump_json()),
                source=source,
            )
            if not written:
                return False
            # Someone has looked again, which is what a conflict was asking
            # for. Whichever way they went, it is settled.
            self._clear_conflict(conn, sample_id, label_set_id)
            self._reindex_classes(
                conn, sample_id, label_set_id, schema.classes_asserted(value)
            )
        return True

    def skip(self, sample_id: int, label_set_id: int, source: str = t.HUMAN) -> bool:
        """Mark a sample reviewed with nothing applicable.

        Excluded from datasets and from the review queue alike, so it does
        not come back round. Returns whether it was recorded, on the same
        terms as :meth:`annotate`.
        """
        with self.engine.begin() as conn:
            if not self._upsert_annotation(
                conn, sample_id, label_set_id, state=t.SKIPPED, value=None, source=source
            ):
                return False
            self._reindex_classes(conn, sample_id, label_set_id, set())
        return True

    def annotate_many(
        self,
        label_set_id: int,
        items: Iterable[tuple[int, AnyValue | None]],
        source: str = t.HUMAN,
        on_item: Callable[[int], None] | None = None,
    ) -> "AnnotateReport":
        """Record many annotations in one transaction.

        A ``None`` value means skipped: no answer, as against an empty
        value, which is the answer "nothing here". An item landing on an
        answer from a source that outranks this one is left alone and
        counted in ``kept`` (:data:`tables.AUTHORITY`).
        """
        _, schema = self._schema(label_set_id)
        annotated = skipped = kept = 0
        with self.engine.begin() as conn:
            for sample_id, value in items:
                if value is None:
                    if self._upsert_annotation(
                        conn, sample_id, label_set_id, state=t.SKIPPED, value=None, source=source
                    ):
                        self._reindex_classes(conn, sample_id, label_set_id, set())
                        skipped += 1
                    else:
                        kept += 1
                else:
                    schema.validate_value(value)
                    if self._upsert_annotation(
                        conn,
                        sample_id,
                        label_set_id,
                        state=t.ANNOTATED,
                        value=json.loads(value.model_dump_json()),
                        source=source,
                    ):
                        self._reindex_classes(
                            conn, sample_id, label_set_id, schema.classes_asserted(value)
                        )
                        annotated += 1
                    else:
                        kept += 1
                if on_item is not None:
                    on_item(sample_id)
        return AnnotateReport(annotated, skipped, kept)

    def unskip(self, label_set_id: int, sample_ids: Iterable[int]) -> int:
        """Return skipped samples to the queue; returns how many moved.

        Deletes the row, since unlabelled is the absence of one; anything
        annotated is left alone. See ``docs/adr/0009``.
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

        For candidates that were never answers, such as an unreviewed
        import. The source has to be named, so a person's answer is never
        removed by a call that meant something else. See ``docs/adr/0009``.
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

    def _upsert_annotation(self, conn, sample_id, label_set_id, *, state, value, source) -> bool:
        """Write one annotation, unless what is there outranks ``source``.

        Returns whether it wrote. See :data:`tables.AUTHORITY`: an import
        landing on a sample a person already answered leaves the answer
        alone rather than replacing it with the guess it may have corrected.
        """
        if source not in t.AUTHORITY:
            raise CatalogError(
                f"Unknown annotation source {source!r}; expected one of "
                f"{', '.join(t.SOURCES)}. Which answer may replace which depends "
                f"on it, so an unrecognised one cannot be ranked."
            )
        where = and_(
            t.annotation.c.sample_id == sample_id,
            t.annotation.c.label_set_id == label_set_id,
        )
        existing = conn.execute(select(t.annotation.c.source).where(where)).first()
        if existing is not None:
            if t.AUTHORITY.get(existing.source, 0) > t.AUTHORITY[source]:
                return False
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
        return True

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
                    live(),
                    t.annotation_conflict.c.label_set_id == label_set_id,
                )
            )
        )
        def value(raw):
            return None if raw is None else VALUE.validate_python(raw)

        with self.engine.connect() as conn:
            return [
                {
                    "sample_id": row.id,
                    "checksum": row.checksum,
                    "kept": value(row.kept_value),
                    "other": value(row.other_value),
                    "origin": row.other_origin,
                }
                for row in conn.execute(scoped(stmt, collections))
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
        return VALUE.validate_python(row.value)


    def digest(self, conn, label_set_id: int, sample_ids: Sequence[int]) -> str:
        """What this label set currently says about these samples, as a digest.

        Over state, source and value, ordered by sample id and serialised
        with sorted keys, so it is a function of the answers alone. See
        ``docs/adr/0003``.
        """
        digest = hashlib.sha256()
        for chunk in chunks(list(sample_ids), 500):
            rows = conn.execute(
                select(
                    t.annotation.c.sample_id,
                    t.annotation.c.state,
                    t.annotation.c.source,
                    t.annotation.c.value,
                )
                .where(
                    and_(
                        t.annotation.c.label_set_id == label_set_id,
                        t.annotation.c.sample_id.in_(chunk),
                    )
                )
                .order_by(t.annotation.c.sample_id)
            ).all()
            for sample_id, state, source, value in rows:
                digest.update(
                    json.dumps(
                        [sample_id, state, source, value],
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                )
        return digest.hexdigest()
