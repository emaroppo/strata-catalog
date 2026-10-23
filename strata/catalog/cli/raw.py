"""Commands for raw files and the runs that wrote them. See ``docs/adr/0045``."""

from dataclasses import asdict
from pathlib import Path

from ._shared import Refused, _emit, _open, _ticker


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except OSError as e:
        raise Refused(f"Cannot read the file list {path}: {e.strerror}") from None


def _ingest(args) -> int:
    from ..rows import CatalogError

    catalog, _ = _open(args, create=True)
    files = _lines(args.files) if args.files is not None else None
    show = _ticker("hashing")
    try:
        report = catalog.ingest_raw(
            args.root, args.collection, files, on_file=lambda d, n: show(f"{d:,}/{n:,}")
        )
    except CatalogError as e:
        raise Refused(str(e)) from None

    def render(r):
        yield f"run {r.run_id}"
        yield f"  {r.files:,} file(s): {r.added:,} added, {r.held:,} already held"
        yield f"  {r.checksums:,} distinct checksum(s), {r.blobs:,} stored anew"

    _emit(args, asdict(report), lambda _: render(report))
    return 0


def _raw_ls(args) -> int:
    catalog, _ = _open(args)
    if args.collection is None:
        held = catalog.raw.collections()

        def render(_):
            if not held:
                yield "No raw files yet."
            for name, count in held.items():
                yield f"{name:<40} {count:>9,}"

        _emit(args, held, render)
        return 0

    rows = catalog.raw.names(args.collection)
    if not args.json:
        # Names only, one a line: a prepare run's file list, one pipe away
        for row in rows:
            print(row.name)
        return 0
    _emit(args, [{"name": row.name, "checksum": row.checksum} for row in rows], None)
    return 0


def _runs(args) -> int:
    catalog, _ = _open(args)
    runs = catalog.runs.every(args.kind)

    def render(_):
        if not runs:
            yield "No runs yet."
        for run in runs:
            counts = ", ".join(
                f"{key} {value:,}" for key, value in run.counts.items() if isinstance(value, int)
            )
            yield f"{run.id}  {run.kind:<8} {run.producer}  {counts}"

    _emit(args, [asdict(run) for run in runs], render)
    return 0


def _verify(args) -> int:
    from ..rows import CatalogError
    from ..verify import verify_run

    catalog, _ = _open(args)
    try:
        report = verify_run(catalog, args.run_id, deep=args.deep)
    except CatalogError as e:
        raise Refused(str(e)) from None

    def render(_):
        yield f"run {report.run_id} ({report.kind})"
        for check in report.checks:
            yield f"  {'ok ' if check.ok else 'BAD'}  {check.name:<8} {check.detail}"
        yield "Holds." if report.ok else "Does not hold."

    _emit(args, {**asdict(report), "ok": report.ok}, render)
    return 0 if report.ok else 1
