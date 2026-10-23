"""Ingesting raw files: a directory's files into the store, as they are.

A one-off, apart from preparation. The files are hashed before anything is
written, so a name already held with other bytes, or a listed file that is
not there, refuses the whole ingest with every such name listed. Then one
transaction opens the run, stores each new checksum once, writes the raw
rows and closes the run with its counts. Run again over the same files,
it writes nothing but the run. See ``docs/adr/0045``.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath

from .index.raw import check_collection
from .index.runs import INGEST
from .rows import CatalogError
from .storage.blobs import checksum_of

#: How many offending names a refusal prints before it counts the rest.
_SHOWN = 20


@dataclass(frozen=True)
class IngestReport:
    """What an ingest did."""

    run_id: str
    collection: str
    #: Names the ingest was handed, and of those, how many it added.
    files: int
    added: int
    #: Names already held with the same bytes, which wrote nothing.
    held: int
    #: Distinct checksums among the files, and how many were stored anew.
    checksums: int
    blobs: int

    def counts(self) -> dict[str, int]:
        return {
            "files": self.files,
            "added": self.added,
            "held": self.held,
            "checksums": self.checksums,
            "blobs": self.blobs,
        }


def listing(names: list[str]) -> str:
    """Names for a refusal: the first few, then how many more."""
    shown = [f"  {name}" for name in names[:_SHOWN]]
    if len(names) > _SHOWN:
        shown.append(f"  ... and {len(names) - _SHOWN:,} more")
    return "\n".join(shown)


def producer() -> tuple[str, dict]:
    """What is writing, and its code identity."""
    try:
        here = version("strata-catalog")
    except PackageNotFoundError:
        here = "unknown"
    return "strata-catalog", {"strata-catalog": here}


def files_under(root: Path, files: Iterable[str] | None) -> list[str]:
    """The paths to ingest, relative to ``root`` and in POSIX form, sorted.

    Every regular file under the root when no list is given. A listed path
    is relative to the root, or absolute inside it; one outside it, or not
    there, refuses the ingest.
    """
    if files is None:
        return sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and not p.is_symlink()
        )

    paths: dict[str, None] = {}
    outside: list[str] = []
    absent: list[str] = []
    for line in files:
        entry = line.strip()
        if not entry:
            continue
        path = Path(entry)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(root)
            except ValueError:
                outside.append(entry)
                continue
        relative = PurePosixPath(path.as_posix())
        if ".." in relative.parts:
            outside.append(entry)
            continue
        if not (root / relative).is_file():
            absent.append(entry)
            continue
        paths[relative.as_posix()] = None

    if outside:
        raise CatalogError(
            f"{len(outside):,} listed path(s) are not under {root}, so they would have no "
            f"name in the collection:\n{listing(outside)}"
        )
    if absent:
        raise CatalogError(
            f"{len(absent):,} listed file(s) are not under {root}:\n{listing(absent)}\n"
            f"Nothing was ingested."
        )
    return sorted(paths)


def ingest_raw(
    catalog,
    root: Path,
    collection: str,
    files: Iterable[str] | None = None,
    on_file: Callable[[int, int], None] | None = None,
) -> IngestReport:
    """Store ``root``'s files and write a raw row for each, under ``collection``."""
    check_collection(collection)
    root = Path(root).resolve()
    if not root.is_dir():
        raise CatalogError(f"{root} is not a directory.")
    paths = files_under(root, files)

    # Hashed first and outside the transaction, so a refusal writes nothing
    hashed: dict[str, str] = {}
    for done, path in enumerate(paths, 1):
        hashed[path] = checksum_of(root / path)
        if on_file is not None:
            on_file(done, len(paths))

    name, code = producer()
    with catalog.engine.begin() as conn:
        held = catalog.raw.held(conn, collection, list(hashed))
        changed = sorted(p for p, c in hashed.items() if p in held and held[p] != c)
        if changed:
            raise CatalogError(
                f"{len(changed):,} name(s) in {collection} are held with other bytes, and a "
                f"name means one set of bytes:\n"
                f"{listing([f'{collection}/{p}' for p in changed])}\n"
                f"A changed source goes into a new collection. Nothing was ingested."
            )
        new = {p: c for p, c in hashed.items() if p not in held}

        run_id = catalog.runs.start(
            conn, kind=INGEST, producer=name, code=code, spec={"collection": collection}
        )
        catalog.runs.given(conn, run_id, hashed.values())
        stored = catalog.raw.stored(conn, sorted(set(new.values())))
        rows = []
        blobs = 0
        for path, checksum in new.items():
            location = stored.get(checksum)
            if location is None:
                location = stored[checksum] = catalog.blobs.put(root / path, checksum)
                blobs += 1
            rows.append((path, checksum, location))
        catalog.raw.add(conn, collection, rows, run_id)

        report = IngestReport(
            run_id=run_id,
            collection=collection,
            files=len(hashed),
            added=len(new),
            held=len(hashed) - len(new),
            checksums=len(set(hashed.values())),
            blobs=blobs,
        )
        catalog.runs.finish(conn, run_id, report.counts())
        # Before the commit: rows naming a shard that failed to upload
        # would be worse than no rows. docs/adr/0002
        catalog.blobs.flush()
    return report
