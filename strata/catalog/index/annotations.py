"""Annotations: what was said about a sample, by whom, and what was said before.

Append-only. A write never changes a row: it stamps the current one as
superseded and adds a new one. Writes honour :data:`tables.AUTHORITY`: a
write never replaces an answer from a source that outranks it. Unlabelled
is the absence of a current row. Each row's classes are indexed through
the indexing contract in :mod:`strata.contracts`, and kept with the row.
See ``docs/adr/0027`` and ``docs/adr/0050``. Disputes live in :mod:`conflicts`; the reads a version
or a feature makes over answers live in :mod:`answers`.
"""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.engine import Engine

from strata.contracts import AnySchema, AnyValue

from ..rows import SCHEMA, VALUE, AnnotateReport, CatalogError, current
from . import conflicts
from . import tables as t


@dataclass(frozen=True)
class Answer:
    """One thing that was said about a sample, as the history reads back."""

    state: str
    value: AnyValue | None
    source: str
    batch: str | None
    created_at: datetime | None
    #: None while this is the current answer.
    superseded_at: datetime | None

    @property
    def current(self) -> bool:
        return self.superseded_at is None


@dataclass(frozen=True)
class ReviewCounts:
    """How one import batch fared under review."""

    #: Confirmed unchanged by a person.
    accepted: int = 0
    #: Replaced by a person's different answer, or skipped.
    corrected: int = 0
    #: Still the current answer, nobody has looked.
    pending: int = 0

    @property
    def reviewed(self) -> int:
        return self.accepted + self.corrected


class Annotations:
    """The catalog's annotations: the current answer per sample per label set, and its past."""

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
        batch: str | None = None,
    ) -> bool:
        """Record what a sample is, and index the classes it asserts.

        Returns whether it was recorded: not when a source outranking this
        one already answered (see :data:`tables.AUTHORITY`). ``batch``
        names the import this arrived in; a write that names none inherits
        the batch of the answer it replaces. See ``docs/adr/0027``.
        """
        _, schema = self._schema(label_set_id)
        schema.validate_value(value)
        with self.engine.begin() as conn:
            written = self._write(
                conn,
                sample_id,
                label_set_id,
                state=t.ANNOTATED,
                value=json.loads(value.model_dump_json()),
                source=source,
                batch=batch,
            )
            if written is None:
                return False
            # Someone has looked again, which is what a conflict was asking
            # for. Whichever way they went, it is settled.
            conflicts.clear(conn, sample_id, label_set_id)
            self._index_classes(conn, written, schema.classes_asserted(value))
        return True

    def skip(
        self, sample_id: int, label_set_id: int, source: str = t.HUMAN, batch: str | None = None
    ) -> bool:
        """Mark a sample reviewed with nothing applicable.

        Excluded from datasets and from the review queue alike, so it does
        not come back round. Returns whether it was recorded, on the same
        terms as :meth:`annotate`.
        """
        with self.engine.begin() as conn:
            written = self._write(
                conn,
                sample_id,
                label_set_id,
                state=t.SKIPPED,
                value=None,
                source=source,
                batch=batch,
            )
        return written is not None

    def annotate_many(
        self,
        label_set_id: int,
        items: Iterable[tuple[int, AnyValue | None]],
        source: str = t.HUMAN,
        on_item: Callable[[int], None] | None = None,
        batch: str | None = None,
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
                    written = self._write(
                        conn,
                        sample_id,
                        label_set_id,
                        state=t.SKIPPED,
                        value=None,
                        source=source,
                        batch=batch,
                    )
                    if written is not None:
                        skipped += 1
                    else:
                        kept += 1
                else:
                    schema.validate_value(value)
                    written = self._write(
                        conn,
                        sample_id,
                        label_set_id,
                        state=t.ANNOTATED,
                        value=json.loads(value.model_dump_json()),
                        source=source,
                        batch=batch,
                    )
                    if written is not None:
                        self._index_classes(conn, written, schema.classes_asserted(value))
                        annotated += 1
                    else:
                        kept += 1
                if on_item is not None:
                    on_item(sample_id)
        return AnnotateReport(annotated, skipped, kept)

    def unskip(self, label_set_id: int, sample_ids: Iterable[int]) -> int:
        """Return skipped samples to the queue; returns how many moved.

        Stamps the skip as superseded with nothing after it; the skip stays
        in the history. Anything annotated is left alone. A change in place,
        so it is an event as well. See ``docs/adr/0027`` and
        ``docs/adr/0050``.
        """
        moved = 0
        with self.engine.begin() as conn:
            for sample_id in sample_ids:
                skip = conn.execute(
                    select(t.annotation.c.id).where(
                        and_(
                            t.annotation.c.sample_id == sample_id,
                            t.annotation.c.label_set_id == label_set_id,
                            t.annotation.c.state == t.SKIPPED,
                            current(),
                        )
                    )
                ).scalar_one_or_none()
                if skip is None:
                    continue
                conn.execute(
                    update(t.annotation)
                    .where(t.annotation.c.id == skip)
                    .values(superseded_at=func.now())
                )
                conn.execute(
                    insert(t.event).values(
                        kind=t.WITHDRAWN, sample_id=sample_id, annotation_id=skip
                    )
                )
                moved += 1
        return moved

    def _write(
        self,
        conn,
        sample_id,
        label_set_id,
        *,
        state,
        value,
        source,
        batch,
        run_id: str | None = None,
        created_at: datetime | None = None,
    ) -> int | None:
        """Append one answer, superseding the current one unless it outranks ``source``.

        ``run_id`` names the import or judge run writing it (record 0048).
        ``created_at`` keeps the time an answer carried from another catalog
        was first given; a new answer takes the database's.

        Returns the new row's id, or None if it wrote nothing. See
        :data:`tables.AUTHORITY`: an import
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
            current(),
        )
        existing = conn.execute(
            select(t.annotation.c.id, t.annotation.c.source, t.annotation.c.batch).where(where)
        ).first()
        if existing is not None:
            if t.AUTHORITY.get(existing.source, 0) > t.AUTHORITY[source]:
                return None
            conn.execute(
                update(t.annotation)
                .where(t.annotation.c.id == existing.id)
                .values(superseded_at=func.now())
            )
            if batch is None:
                batch = existing.batch
        return conn.execute(
            insert(t.annotation).values(
                sample_id=sample_id,
                label_set_id=label_set_id,
                state=state,
                value=value,
                source=source,
                batch=batch,
                run_id=run_id,
                **({} if created_at is None else {"created_at": created_at}),
            )
        ).inserted_primary_key[0]

    def _index_classes(self, conn, annotation_id: int, classes: set[str]) -> None:
        """The classes one row asserts, kept with that row. See ``docs/adr/0050``."""
        if classes:
            conn.execute(
                insert(t.annotation_class),
                [{"annotation_id": annotation_id, "class_name": name} for name in sorted(classes)],
            )

    # -- reading ----------------------------------------------------------

    def annotation_of(self, sample_id: int, label_set_id: int) -> AnyValue | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.annotation.c.state, t.annotation.c.value).where(
                    and_(
                        t.annotation.c.sample_id == sample_id,
                        t.annotation.c.label_set_id == label_set_id,
                        current(),
                    )
                )
            ).first()
        if row is None or row.state == t.SKIPPED:
            return None
        # Through the discriminator. docs/adr/0014
        return VALUE.validate_python(row.value)

    def values_of(self, label_set_id: int, sample_ids: Iterable[int]) -> dict[int, AnyValue]:
        """The current answer of each of ``sample_ids`` that has one, by id."""
        found: dict[int, AnyValue] = {}
        ids = list(sample_ids)
        with self.engine.connect() as conn:
            for start in range(0, len(ids), 500):
                rows = conn.execute(
                    select(t.annotation.c.sample_id, t.annotation.c.value).where(
                        and_(
                            t.annotation.c.label_set_id == label_set_id,
                            t.annotation.c.sample_id.in_(ids[start : start + 500]),
                            t.annotation.c.state == t.ANNOTATED,
                            current(),
                        )
                    )
                ).all()
                for sample_id, raw in rows:
                    if raw is not None:
                        found[sample_id] = VALUE.validate_python(raw)
        return found

    def history(self, sample_id: int, label_set_id: int) -> list[Answer]:
        """Everything ever said about a sample under a label set, oldest first.

        The last entry is the current answer, unless it was withdrawn — a
        skip returned to the queue — in which case nothing is.
        """
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    t.annotation.c.state,
                    t.annotation.c.value,
                    t.annotation.c.source,
                    t.annotation.c.batch,
                    t.annotation.c.created_at,
                    t.annotation.c.superseded_at,
                )
                .where(
                    and_(
                        t.annotation.c.sample_id == sample_id,
                        t.annotation.c.label_set_id == label_set_id,
                    )
                )
                .order_by(t.annotation.c.created_at, t.annotation.c.id)
            ).all()
        return [_answer(row) for row in rows]

    def second_looks(self, label_set_id: int) -> tuple[int, int]:
        """How a person's answers fared when a person looked again: (agreed, changed).

        A sample whose current answer is a person's and whose previous one
        was also a person's had a second look — an audit, or a correction
        somebody came back to make. Equal values agree; anything else,
        including a skip after an answer, changed.
        """
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    t.annotation.c.sample_id,
                    t.annotation.c.state,
                    t.annotation.c.value,
                    t.annotation.c.source,
                    t.annotation.c.superseded_at,
                )
                .where(t.annotation.c.label_set_id == label_set_id)
                .order_by(t.annotation.c.sample_id, t.annotation.c.created_at, t.annotation.c.id)
            ).all()
        by_sample: dict[int, list] = {}
        for row in rows:
            by_sample.setdefault(row.sample_id, []).append(row)
        agreed = changed = 0
        for answers in by_sample.values():
            people = [a for a in answers if a.source == t.HUMAN]
            if len(people) < 2 or people[-1].superseded_at is not None:
                continue
            first, second = people[-2], people[-1]
            if (first.state, first.value) == (second.state, second.value):
                agreed += 1
            else:
                changed += 1
        return agreed, changed

    def review_counts(self, label_set_id: int) -> dict[str, ReviewCounts]:
        """How each import batch fared under review, by batch name.

        Per sample: the latest imported answer, and what stands now. Still
        the import, nobody has looked. A person's answer after it, equal in
        value, is an acceptance; a different one, or a skip, a correction.
        See ``docs/adr/0027``.
        """
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    t.annotation.c.sample_id,
                    t.annotation.c.state,
                    t.annotation.c.value,
                    t.annotation.c.source,
                    t.annotation.c.batch,
                    t.annotation.c.superseded_at,
                )
                .where(t.annotation.c.label_set_id == label_set_id)
                .order_by(t.annotation.c.sample_id, t.annotation.c.created_at, t.annotation.c.id)
            ).all()
        tallies: dict[str, dict[str, int]] = {}
        by_sample: dict[int, list] = {}
        for row in rows:
            by_sample.setdefault(row.sample_id, []).append(row)
        for answers in by_sample.values():
            imports = [a for a in answers if a.source == t.IMPORT and a.state == t.ANNOTATED]
            if not imports:
                continue
            latest = imports[-1]
            batch = latest.batch or ""
            tally = tallies.setdefault(batch, {"accepted": 0, "corrected": 0, "pending": 0})
            standing = next((a for a in answers if a.superseded_at is None), None)
            if standing is None or standing is latest:
                tally["pending"] += 1
            elif standing.source == t.HUMAN and standing.state == t.ANNOTATED:
                tally["accepted" if standing.value == latest.value else "corrected"] += 1
            elif standing.source == t.HUMAN:
                tally["corrected"] += 1
            else:
                tally["pending"] += 1
        return {batch: ReviewCounts(**tally) for batch, tally in sorted(tallies.items())}


def _answer(row) -> Answer:
    value = (
        VALUE.validate_python(row.value)
        if row.state == t.ANNOTATED and row.value is not None
        else None
    )
    return Answer(
        state=row.state,
        value=value,
        source=row.source,
        batch=row.batch,
        created_at=row.created_at,
        superseded_at=row.superseded_at,
    )
