"""Disagreement, recorded rather than resolved: two answers for one sample, kept side by side.

Only a merge records one — a reviewer changing their mind is not a
conflict, it is the point of being able to correct an answer — and a fresh
answer clears it, whichever way it went. See ``docs/adr/0009``.
"""

import json

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Engine

from strata.labels import AnyValue

from ..rows import VALUE, live, scoped
from . import tables as t


def clear(conn, sample_id: int, label_set_id: int) -> None:
    """A fresh answer settles it, whichever way it went. Within a caller's transaction."""
    conn.execute(
        delete(t.annotation_conflict).where(
            and_(
                t.annotation_conflict.c.sample_id == sample_id,
                t.annotation_conflict.c.label_set_id == label_set_id,
            )
        )
    )


class Conflicts:
    """The catalog's disputed answers."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def record(
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

    def disputed(self, label_set_id: int, collections) -> list[dict]:
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
