"""What callers see, and the scope every query shares: a record, never a raw row, and
the collections a query draws from. See ``docs/adr/0022``.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import NamedTuple

from pydantic import TypeAdapter
from sqlalchemy import and_, or_, select
from sqlalchemy.sql import ColumnElement

from strata.contracts import AnySchema, AnyValue

from .index import tables as t
from .storage.blobs import Location

SCHEMA = TypeAdapter(AnySchema)
VALUE = TypeAdapter(AnyValue)

#: Every collection, said out loud: a query has to name what it wants. docs/adr/0022
EVERYTHING = "*"


class CatalogError(Exception):
    """A request the catalog cannot honour."""


class CatalogMissing(CatalogError):
    """An index with no catalog in it: nothing to open, and nothing is made."""


class AmbiguousSample(CatalogError):
    """A checksum named without a type, where more than one type holds the bytes.

    See ``docs/adr/0052``.
    """


def split_type(type_: str) -> tuple[str, str]:
    """``text/email/masked`` as its media and stored subtype, ``("text", "email/masked")``.

    See ``docs/adr/0010``, ``docs/adr/0052``.
    """
    media, sep, subtype = type_.partition("/")
    if not media or not sep or not subtype:
        raise CatalogError(
            f"{type_!r} is not a sample type. A type is its media and its subtype, "
            f"such as 'text/email' or 'image/plain'."
        )
    return media, subtype


@dataclass(frozen=True)
class SampleRow:
    """A sample as callers see it, never a raw database row."""

    id: int
    checksum: str
    location: Location
    media: str
    subtype: str
    #: Out of comparison and the hash: two rows for one sample are the same sample. docs/adr/0001
    metadata: dict | None = field(default=None, compare=False)

    @property
    def type(self) -> str:
        """Its media and subtype, ``text/email``: with the checksum, its identity.

        See ``docs/adr/0052``.
        """
        return f"{self.media}/{self.subtype}"


@dataclass(frozen=True)
class RunRow:
    """A run that wrote to the catalog, as callers see it. docs/adr/0045"""

    id: str
    kind: str
    producer: str
    code: dict | None
    spec: dict | None
    spec_hash: str | None
    chain: str | None
    files_digest: str | None
    continues: str | None
    counts: dict
    started_at: datetime
    #: Null for a run whose transaction is still open; a failed one left no row.
    finished_at: datetime | None


@dataclass(frozen=True)
class RawRow:
    """A raw file as callers see it: its name, and where its bytes are."""

    collection: str
    path: str
    checksum: str
    location: Location
    run_id: str

    @property
    def name(self) -> str:
        return f"{self.collection}/{self.path}"


class DatasetRef(NamedTuple):
    """Which dataset version an id is, and what it said when it was frozen."""

    name: str
    version: int
    #: Over the version's members and their annotations; null where the version carries none.
    annotation_digest: str | None


class AnnotateReport(NamedTuple):
    """What a bulk write did."""

    #: Answers recorded.
    annotated: int
    #: Skips recorded.
    skipped: int
    #: Left alone, because a source outranking the writer had already answered. docs/adr/0009
    kept: int


def chunks(items: list, size: int = 500):
    """Batches of ``size``: SQLite caps parameters per statement. See ``docs/adr/0032``."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def within(collections) -> ColumnElement[bool] | None:
    """A condition matching samples in any of ``collections``, or ``None`` for everything.

    A collection covers what is under it: ``a`` covers ``a/b``, never ``a_old``. See
    ``docs/adr/0022``.
    """
    names = [collections] if isinstance(collections, str) else list(collections)
    if names == [EVERYTHING]:
        # Bare or as a list's only member: a config carries a list, meaning the same
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


def current():
    """Annotation rows that are the answer now, rather than what it used to be."""
    return t.annotation.c.superseded_at.is_(None)


def scoped(stmt, collections):
    """Restrict a sample query to the collections a caller named, by EXISTS rather than a join.

    See ``docs/adr/0021``.
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
            metadata=r.metadata,
        )
        for r in conn.execute(stmt)
    ]
