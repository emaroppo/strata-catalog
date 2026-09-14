"""The reads a version or a feature makes over answers: their digest, and the classes asserted."""

import hashlib
import json
from collections.abc import Sequence

from sqlalchemy import and_, select

from strata.labels import AnySchema

from ..rows import VALUE, chunks, current
from . import tables as t


def digest(conn, label_set_id: int, sample_ids: Sequence[int]) -> str:
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
                    current(),
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


def asserted(
    conn, label_set_id: int, schema: AnySchema, sample_ids: Sequence[int]
) -> dict[int, list[str]]:
    """The classes each sample's answer asserts, through the schema's indexing contract.

    Read that way rather than by reaching into a payload this does not
    understand — the same reason a new task type becomes queryable
    without the catalog learning about it. Samples with no answer, or
    an answer asserting nothing, are absent.
    """
    found: dict[int, list[str]] = {}
    for chunk in chunks(list(sample_ids)):
        rows = conn.execute(
            select(t.annotation.c.sample_id, t.annotation.c.value).where(
                and_(
                    t.annotation.c.label_set_id == label_set_id,
                    t.annotation.c.state == t.ANNOTATED,
                    t.annotation.c.sample_id.in_(chunk),
                    current(),
                )
            )
        ).all()
        for sample_id, raw in rows:
            if raw is None:
                continue
            asserted = sorted(schema.classes_asserted(VALUE.validate_python(raw)))
            if asserted:
                found[sample_id] = asserted
    return found
