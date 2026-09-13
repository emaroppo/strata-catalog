"""``strata-catalog remove``: tombstone samples, and their annotations with them."""

from ._shared import Refused, _emit, _open


def _remove(args) -> int:
    from ..admin import remove
    from ..rows import CatalogError

    where = {}
    for pair in args.where or ():
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise Refused(f"--where takes key=value, got {pair!r}")
        where[key] = value

    catalog, _ = _open(args)
    try:
        report = remove(
            catalog,
            collections=args.collection or None,
            where=where,
            checksums=args.checksum or None,
            dry_run=args.dry_run,
        )
    except CatalogError as e:
        raise Refused(str(e)) from None

    def render(r):
        yield f"{r.matched:,} live sample(s) match"
        if args.dry_run:
            yield "Dry run: nothing removed. Run again without --dry-run to tombstone them."
        else:
            yield (
                f"{r.removed:,} removed. They leave the queue, the labelled set and every "
                f"version frozen from now on; versions already frozen keep them, and the "
                f"bytes stay until a compaction pass."
            )

    _emit(args, record=report, render=render)
    return 0
