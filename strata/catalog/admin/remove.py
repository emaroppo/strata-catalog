"""Removing data: samples tombstoned, and their annotations gone with them.

A removed sample leaves every reader and its annotations leave with it.
The bytes stay, and a version already frozen keeps its members. See
``docs/adr/0002``.
"""

from ..catalog import Catalog
from ..rows import CatalogError
from .where import Strict


class RemoveReport(Strict):
    """What a removal matched, and what it did."""

    #: Live samples the selection named.
    matched: int
    #: How many are not live now. Zero on a dry run.
    removed: int
    #: The selection, as the caller stated it.
    collections: list[str]
    where: dict[str, str]
    checksums: int


def remove(
    catalog: Catalog,
    *,
    collections=None,
    where: dict[str, str] | None = None,
    checksums: list[str] | None = None,
    dry_run: bool = False,
) -> RemoveReport:
    """Tombstone the live samples a selection names.

    A selection is at least one of: the collections to look in, metadata
    values to match, checksums to name outright. With none of them it is
    refused. See ``docs/adr/0002``.
    """
    where = dict(where or {})
    named = set(checksums or ())
    if not collections and not where and not named:
        raise CatalogError(
            "Removing needs a selection: --collection, --where key=value, or --checksum. "
            "With none, this would name the whole catalog."
        )
    rows = catalog.samples.matching(collections or ["*"], where)
    if named:
        rows = [row for row in rows if row.checksum in named]
    removed = 0 if dry_run else catalog.samples.tombstone([row.id for row in rows])
    return RemoveReport(
        matched=len(rows),
        removed=removed,
        collections=list(collections or []),
        where=where,
        checksums=len(named),
    )
