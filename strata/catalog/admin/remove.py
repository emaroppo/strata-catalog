"""Removing data: samples tombstoned, and their annotations gone with them.

A removed sample leaves every reader and its annotations leave with it.
The bytes stay, and a version already frozen keeps its members. See
``docs/adr/0002``. Another type's sample over the same bytes is another
sample, and stays. See ``docs/adr/0052``.
"""

from pydantic import Field

from ..catalog import Catalog
from ..rows import CatalogError, split_type
from .where import Strict


class RemoveReport(Strict):
    """What a removal matched, and what it did."""

    #: Live samples the selection named.
    matched: int
    #: How many are not live now. Zero on a dry run.
    removed: int
    #: The selection, as the caller stated it.
    collections: list[str]
    #: Compared with their types: 2000 is a number. docs/adr/0050
    where: dict[str, bool | int | float | str]
    checksums: int
    #: The types the selection kept to, ``text/email``; none means any.
    types: list[str] = Field(default_factory=list)


def remove(
    catalog: Catalog,
    *,
    collections=None,
    where: dict | None = None,
    checksums: list[str] | None = None,
    types: list[str] | None = None,
    dry_run: bool = False,
) -> RemoveReport:
    """Tombstone the live samples a selection names.

    A selection is at least one of: the collections to look in, metadata
    values to match, checksums to name outright. With none of them it is
    refused. See ``docs/adr/0002``. ``types`` keeps it to samples of those
    types. A checksum names a sample outright only with its type, so one
    that matched samples of several types, with no type given, is refused
    rather than taking them all. See ``docs/adr/0052``.
    """
    where = dict(where or {})
    named = set(checksums or ())
    kinds = sorted(set(types or ()))
    if not collections and not where and not named:
        raise CatalogError(
            "Removing needs a selection: --collection, --where key=value, or --checksum. "
            "With none, this would name the whole catalog."
        )
    for kind in kinds:
        split_type(kind)
    rows = catalog.samples.matching(collections or ["*"], where)
    if kinds:
        rows = [row for row in rows if row.type in kinds]
    if named:
        rows = [row for row in rows if row.checksum in named]
        held: dict[str, set[str]] = {}
        for row in rows:
            held.setdefault(row.checksum, set()).add(row.type)
        torn = sorted(c for c, held_as in held.items() if len(held_as) > 1)
        if torn and not kinds:
            raise CatalogError(
                f"{len(torn)} checksum(s) are held by samples of several types, such as "
                f"{torn[0]} as {', '.join(sorted(held[torn[0]]))}. Say which with --type; "
                f"nothing was removed."
            )
    removed = 0 if dry_run else catalog.samples.tombstone([row.id for row in rows])
    return RemoveReport(
        matched=len(rows),
        removed=removed,
        collections=list(collections or []),
        where=where,
        checksums=len(named),
        types=kinds,
    )
