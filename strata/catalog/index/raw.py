"""Raw files: what arrived, by name, and where its bytes are.

Append-only. A name is a collection and a path under the root an ingest was
given, and it means one set of bytes; the same bytes under several names
are several rows over one blob. Nothing here reads a file. See
``docs/adr/0045``.
"""

import re
from collections.abc import Iterable, Sequence

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine

from ..rows import CatalogError, RawRow, chunks
from ..storage.blobs import Location
from . import tables as t

#: A checksum in a list of names stands for itself.
_CHECKSUM = re.compile(r"[0-9a-f]{64}")


def check_collection(collection: str) -> str:
    """A collection as a raw row holds it: segments, no empty one."""
    if not collection or any(not part for part in collection.split("/")):
        raise CatalogError(
            f"{collection!r} is not a collection: give a path such as enron/maildir, "
            f"with no leading, trailing or doubled slash."
        )
    return collection


class Raw:
    """The catalog's raw files."""

    def __init__(self, engine: Engine):
        self.engine = engine

    # -- within a caller's transaction ----------------------------------

    def held(self, conn, collection: str, paths: Sequence[str]) -> dict[str, str]:
        """The checksum each of ``paths`` already names in ``collection``."""
        found: dict[str, str] = {}
        for chunk in chunks(list(paths)):
            found.update(
                (row.path, row.checksum)
                for row in conn.execute(
                    select(t.raw.c.path, t.raw.c.checksum).where(
                        t.raw.c.collection == collection, t.raw.c.path.in_(chunk)
                    )
                )
            )
        return found

    def add(
        self, conn, collection: str, rows: Iterable[tuple[str, str, Location]], run_id: str
    ) -> int:
        """New rows, as (path, checksum, location), for bytes already stored."""
        values = [
            {
                "collection": collection,
                "path": path,
                "checksum": checksum,
                "location": location.container,
                "offset": location.offset,
                "length": location.length,
                "run_id": run_id,
            }
            for path, checksum, location in rows
        ]
        for chunk in chunks(values):
            conn.execute(insert(t.raw), chunk)
        return len(values)

    def stored(self, conn, checksums: Sequence[str]) -> dict[str, Location]:
        """Where the catalog already keeps each of these checksums' bytes.

        A sample's or a raw row's, whichever it finds: the store holds one
        copy per checksum, so a put looks here first. docs/adr/0045
        """
        found: dict[str, Location] = {}
        for chunk in chunks(list(checksums)):
            for table in (t.sample, t.raw):
                for row in conn.execute(
                    select(
                        table.c.checksum, table.c.location, table.c.offset, table.c.length
                    ).where(table.c.checksum.in_(chunk))
                ):
                    found.setdefault(row.checksum, Location(row.location, row.offset, row.length))
        return found

    def resolve(self, conn, names: Iterable[str]) -> tuple[dict[str, str], list[str]]:
        """Each name's checksum, and the names nothing ingested.

        A name is ``collection/path``. A collection has segments, so where
        it ends is found among the collections the catalog holds, longest
        first. A checksum stands for itself if a raw row holds it.
        """
        wanted = list(dict.fromkeys(name.strip() for name in names if name.strip()))
        collections = sorted(
            conn.execute(select(t.raw.c.collection).distinct()).scalars(), key=len, reverse=True
        )
        by_collection: dict[str, dict[str, str]] = {}
        checksums: list[str] = []
        unplaced: list[str] = []
        for name in wanted:
            if _CHECKSUM.fullmatch(name):
                checksums.append(name)
                continue
            for collection in collections:
                if name.startswith(collection + "/"):
                    by_collection.setdefault(collection, {})[name[len(collection) + 1 :]] = name
                    break
            else:
                unplaced.append(name)

        found: dict[str, str] = {}
        for collection, paths in by_collection.items():
            for path, checksum in self.held(conn, collection, list(paths)).items():
                found[paths[path]] = checksum
        for chunk in chunks(checksums):
            for checksum in conn.execute(
                select(t.raw.c.checksum).where(t.raw.c.checksum.in_(chunk)).distinct()
            ).scalars():
                found[checksum] = checksum
        missing = [name for name in wanted if name not in found]
        return found, missing

    # -- reads ----------------------------------------------------------

    def names(self, collection: str) -> list[RawRow]:
        """A collection's raw rows, by path."""
        with self.engine.connect() as conn:
            return [
                RawRow(
                    collection=row.collection,
                    path=row.path,
                    checksum=row.checksum,
                    location=Location(row.location, row.offset, row.length),
                    run_id=row.run_id,
                )
                for row in conn.execute(
                    select(t.raw).where(t.raw.c.collection == collection).order_by(t.raw.c.path)
                )
            ]

    def collections(self) -> dict[str, int]:
        """Each raw collection, and how many names it holds."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(t.raw.c.collection, func.count())
                .group_by(t.raw.c.collection)
                .order_by(t.raw.c.collection)
            )
            # .all(): a result has keys(), so dict() would read it as a mapping
            return dict(rows.tuples().all())
