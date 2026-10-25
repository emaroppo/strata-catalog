"""Lists: sets of checksums with a name and a version; a version never changes, and
records the one type its members are. See ``docs/adr/0050``, ``docs/adr/0052``.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, insert, select
from sqlalchemy.engine import Engine

from ..rows import CatalogError, chunks, split_type
from . import tables as t
from .runs import files_digest


@dataclass(frozen=True)
class ListRow:
    name: str
    version: int
    size: int
    digest: str
    created_at: datetime | None
    #: ``text/email``, or None for a list put without a type.
    type: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


def list_digest(checksums: Iterable[str]) -> str:
    """Over the sorted distinct checksums: :func:`runs.files_digest`, so one set has one digest."""
    return files_digest(checksums)


def check_name(name: str) -> None:
    if not name or "@" in name or name != name.strip():
        raise CatalogError(
            f"{name!r} is not a list name: a name has no '@', which separates its version."
        )


class Lists:
    """The catalog's lists."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def put(
        self, name: str, checksums: Iterable[str], type: str | None = None
    ) -> tuple[ListRow, bool]:
        """Store a set under ``name``: the version holding it, and whether it is new.

        The same set as the latest version is that version unless ``type`` differs;
        a checksum need not be a sample yet. See ``docs/adr/0052``.
        """
        check_name(name)
        if type is not None:
            split_type(type)
        wanted = sorted(set(checksums))
        bad = [c for c in wanted if len(c) != 64 or any(ch not in "0123456789abcdef" for ch in c)]
        if bad:
            raise CatalogError(f"Not a sha256 checksum: {', '.join(bad[:5])}")
        digest = list_digest(wanted)
        with self.engine.begin() as conn:
            latest = self._latest(conn, name)
            if latest is not None and latest.digest == digest and type in (None, latest.type):
                return latest, False
            version = 1 if latest is None else latest.version + 1
            return self.add(conn, name, version, digest, wanted, type), True

    def add(
        self,
        conn,
        name: str,
        version: int,
        digest: str,
        checksums: list[str],
        type: str | None = None,
    ) -> ListRow:
        """Write one version as given, within a caller's transaction."""
        media, subtype = (None, None) if type is None else split_type(type)
        list_id = conn.execute(
            insert(t.list_version).values(
                name=name,
                version=version,
                digest=digest,
                size=len(checksums),
                media=media,
                subtype=subtype,
            )
        ).inserted_primary_key[0]
        for chunk in chunks([{"list_id": list_id, "checksum": c} for c in checksums]):
            conn.execute(insert(t.list_member), chunk)
        row = conn.execute(select(t.list_version).where(t.list_version.c.id == list_id)).one()
        return _row(row)

    def every(self) -> list[ListRow]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(t.list_version).order_by(t.list_version.c.name, t.list_version.c.version)
            ).all()
        return [_row(row) for row in rows]

    def get(self, name: str, version: int) -> tuple[ListRow, list[str]]:
        """A version and its checksums, sorted."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.list_version).where(
                    and_(t.list_version.c.name == name, t.list_version.c.version == version)
                )
            ).first()
            if row is None:
                raise CatalogError(f"No list {name}@{version} in this catalog.")
            members = sorted(
                conn.execute(
                    select(t.list_member.c.checksum).where(t.list_member.c.list_id == row.id)
                ).scalars()
            )
        return _row(row), members

    def _latest(self, conn, name: str) -> ListRow | None:
        row = conn.execute(
            select(t.list_version)
            .where(t.list_version.c.name == name)
            .order_by(t.list_version.c.version.desc())
            .limit(1)
        ).first()
        return None if row is None else _row(row)


def _row(row) -> ListRow:
    return ListRow(
        row.name,
        row.version,
        row.size,
        row.digest,
        row.created_at,
        None if row.media is None else f"{row.media}/{row.subtype}",
    )
