"""Commands for work away from the index: copy it out, merge answers back."""

from ..admin.where import redacted
from ..catalog import Catalog
from ..config import blobs_for
from ..sync.copy import copy_index
from ..sync.merge import merge_annotations
from ._shared import _emit, _open, _ticker


def _copy(args) -> int:
    source, config = _open(args)
    # Only the index moves. docs/adr/0002
    target = Catalog.create(args.to, blobs_for(config))
    show = _ticker("copying")
    report = copy_index(source, target, on_progress=lambda table, n: show(f"{table} ({n:,})"))

    def render(_):
        for name, count in report.copied.items():
            yield f"  {name:<18} {count:>9,}"
        yield f"{report.total:,} row(s) copied"
        yield "Blobs were not touched. Point [catalog] url at the new index; leave root as it is."

    _emit(args, {"copied": report.copied, "total": report.total}, render)
    return 0


def _merge(args) -> int:
    target, config = _open(args)
    # A copy is the same corpus, reading the same bytes. docs/adr/0008
    source = Catalog.connect(args.source, blobs_for(config))
    show = _ticker("merging")
    report = merge_annotations(
        source, target, dry_run=not args.apply, on_progress=lambda d, n: show(f"{d:,}/{n:,}")
    )

    payload = {
        "copied": report.copied,
        "agreed": report.agreed,
        "conflicted": report.conflicted,
        "skipped": report.skipped,
        "superseded": report.superseded,
        "confirmed": report.confirmed,
        "outranked": report.outranked,
        "unknown_samples": len(report.unknown_samples),
        "unknown_label_sets": report.unknown_label_sets,
        "written": report.written if args.apply else 0,
        "applied": args.apply,
    }

    def render(_):
        if not report.total and not report.unknown_label_sets:
            yield f"Nothing to merge from {redacted(args.source)}."
            return
        for line in report.lines():
            yield f"  {line}"
        if report.unknown_samples:
            yield (
                "Some answers are for samples this catalog has never seen: ingested on the "
                "other machine after the copy was taken. Ingest those files here and merge again."
            )
        if not args.apply:
            yield "Nothing was written. Re-run with --apply."
            return
        yield f"{report.written:,} annotation(s) merged"
        if report.conflicted:
            yield (
                f"{report.conflicted:,} sample(s) were answered both ways. This catalog kept "
                f"its own; the next push sends them for review ahead of everything else."
            )

    _emit(args, payload, render)
    return 0
