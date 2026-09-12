"""What callers see, and the scope every query shares.

A caller never sees a raw database row; it sees one of the records here.
And every query over samples names the collections it draws from, through
:func:`scoped`, so "all of it" is a thing you choose rather than a thing
you get by omission.
"""

from dataclasses import dataclass, field
from typing import NamedTuple

from pydantic import TypeAdapter
from sqlalchemy import and_, or_, select

from strata.labels import AnySchema, AnyValue

from .index import tables as t
from .storage.blobs import Location

SCHEMA = TypeAdapter(AnySchema)
VALUE = TypeAdapter(AnyValue)

#: Every collection, said out loud. A query has to name what it wants —
#: forgetting to scope one is how a project's review queue fills with
#: another project's data — so this exists to make "all of it" a thing you
#: choose rather than a thing you get by omission.
EVERYTHING = "*"


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


class DatasetRef(NamedTuple):
    """Which dataset version an id is, and what it said when it was frozen."""

    name: str
    version: int
    #: Over the version's members and their annotations. Null for a version
    #: frozen before versions carried one.
    annotation_digest: str | None


class AnnotateReport(NamedTuple):
    """What a bulk write did."""

    #: Answers recorded.
    annotated: int
    #: Skips recorded.
    skipped: int
    #: Left alone, because a source outranking the one writing had already
    #: answered — an import landing on a sample a person dealt with.
    kept: int


def chunks(items: list, size: int = 500):
    """SQLite caps parameters per statement, and a review pool is past it.

    The cap depends on the interpreter: an apt-installed Python allows
    250,000 bound parameters and a uv-managed one 32,766, so a corpus that
    held fine on one machine failed on another.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]


def within(collections) -> object | None:
    """A condition matching samples in any of ``collections``.

    ``None`` when the answer is everything, so a caller can drop the join
    entirely rather than filter on a tautology.

    Selecting a collection selects what is under it: ``sat_images`` covers
    ``sat_images/2024`` but never ``sat_images_old``, which a bare prefix
    match would swallow. That distinction lives here rather than at each
    call site.
    """
    names = [collections] if isinstance(collections, str) else list(collections)
    if names == [EVERYTHING]:
        # The marker alone, bare or as a list's only member: a request that
        # arrived through a config carries a list, and means the same thing
        return None
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


def live():
    """Samples that have not been discarded."""
    return t.sample.c.deleted_at.is_(None)


def scoped(stmt, collections):
    """Restrict a sample query to the collections a caller named.

    An EXISTS rather than a join, because a sample in three collections
    would otherwise come back three times and need a DISTINCT to fix —
    and Postgres cannot take DISTINCT over a json column at all, so the
    obvious shape fails on one dialect and silently duplicates on the
    other.
    """
    condition = within(collections)
    if condition is None:
        return stmt
    return stmt.where(
        select(t.sample_collection.c.sample_id)
        .where(and_(t.sample_collection.c.sample_id == t.sample.c.id, condition))
        .exists()
    )


SAMPLE_COLUMNS = (
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


def sample_rows(conn, stmt) -> list[SampleRow]:
    """``stmt`` selects :data:`SAMPLE_COLUMNS`; this is its rows as callers see them."""
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
