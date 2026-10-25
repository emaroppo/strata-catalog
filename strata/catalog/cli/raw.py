"""Commands for raw files and the runs that wrote them. See ``docs/adr/0045``."""

import sys
from dataclasses import asdict
from pathlib import Path

from ..prepare_run import load_chain, prepare
from ..verify import verify_run
from ._shared import Refused, _emit, _open, _ticker


def _lines(path: Path) -> list[str]:
    if str(path) == "-":
        return sys.stdin.read().splitlines()
    try:
        return path.read_text().splitlines()
    except OSError as e:
        raise Refused(f"Cannot read the file list {path}: {e.strerror}") from None


def _ingest(args) -> int:
    # Read before the catalog is opened: an ingest makes one, and a refusal must leave none
    files = _lines(args.files) if args.files is not None else None
    catalog, _ = _open(args, create=True)
    show = _ticker("hashing")
    report = catalog.ingest_raw(
        args.root, args.collection, files, on_file=lambda d, n: show(f"{d:,}/{n:,}")
    )

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
    catalog, _ = _open(args)
    report = verify_run(catalog, args.run_id, deep=args.deep)

    def render(_):
        yield f"run {report.run_id} ({report.kind})"
        for check in report.checks:
            yield f"  {'ok ' if check.ok else 'BAD'}  {check.name:<8} {check.detail}"
        yield "Holds." if report.ok else "Does not hold."

    _emit(args, {**asdict(report), "ok": report.ok}, render)
    return 0 if report.ok else 1


def _prepare(args) -> int:
    catalog, _ = _open(args)
    show = _ticker("preparing")
    chain = load_chain(args.spec)
    report = prepare(
        catalog,
        chain,
        _lines(args.files),
        allow_missing=args.allow_missing,
        continues=args.continues,
        on_input=lambda d, n: show(f"{d:,}/{n:,}"),
    )

    def render(_):
        c = report.counts
        yield f"run {report.run_id} into {report.collection} ({report.sample_type})"
        yield f"  {report.names:,} name(s), {c['missing']:,} missing, {report.inputs:,} input(s)"
        yield (
            f"  {c['produced']:,} produced, {c['already']:,} already prepared by this chain, "
            f"{sum(c['emptied'].values()):,} left empty"
        )
        for reason, n in c["emptied"].items():
            yield f"    empty   {reason:<30} {n:>9,}"
        for reason, n in c["dropped"].items():
            yield f"    dropped {reason:<30} {n:>9,}"
        yield f"  {c['samples']:,} sample(s), {c['new_samples']:,} new"
        if "superseded" in c:
            yield (
                f"  supersedes {c['superseded']:,} earlier sample(s), "
                f"{c['superseded_answered']:,} with a person's answer"
            )

    _emit(args, asdict(report), render)
    return 0
