"""``strata-catalog``: looking after a catalog from the command line.

Standard library only — argparse and print — because this ships with the
catalog, and the catalog's install stays as thin as it is. Every command
builds a request, calls the operation and renders the record; ``--json``
renders the record as it is, for anything that would rather read than
parse a table.
"""

import argparse
import json
import sys
from pathlib import Path

from .config import CatalogConfigError, CatalogMissing, load_catalogs, open_catalog

CONFIG = Path("config.toml")


class Refused(Exception):
    """A reason the command stopped, for the user rather than a traceback."""


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.run(args) or 0
    except (Refused, CatalogConfigError) as e:
        print(str(e), file=sys.stderr)
        return 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strata-catalog", description="Look after a catalog: what it holds, and where."
    )
    parser.add_argument(
        "--config", type=Path, default=CONFIG, help="Host settings (default: ./config.toml)"
    )
    parser.add_argument(
        "--catalog", default="", help="Which catalog on this host (default: the host's own)"
    )
    parser.add_argument("--json", action="store_true", help="Print the record as JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="The catalogs this host is configured for").set_defaults(
        run=_list
    )
    commands.add_parser("types", help="The sample types this catalog can ingest").set_defaults(
        run=_types
    )
    commands.add_parser("preparers", help="The conversions installed here").set_defaults(
        run=_preparers
    )
    commands.add_parser("stats", help="What is in the catalog").set_defaults(run=_stats)
    commands.add_parser(
        "probe", help="Prove this host can reach the index and the blobs"
    ).set_defaults(run=_probe)

    copy = commands.add_parser("copy", help="Copy the index into another database")
    copy.add_argument("--to", required=True, metavar="URL", help="Index URL to copy into")
    copy.set_defaults(run=_copy)

    merge = commands.add_parser("merge", help="Fold a copy's annotations back in")
    merge.add_argument("--from", dest="source", required=True, metavar="URL")
    merge.add_argument("--apply", action="store_true", help="Write; otherwise only report")
    merge.set_defaults(run=_merge)

    repack = commands.add_parser("repack", help="Pack local blobs into tar shards in a bucket")
    repack.add_argument("--dry-run", action="store_true", help="Report what would move")
    repack.add_argument("--shard-mb", type=int, default=512, help="Shard size in MB")
    repack.add_argument("--verify", type=int, default=64, help="Members to read back afterwards")
    repack.set_defaults(run=_repack)
    return parser


# ----------------------------------------------------------------------


def _catalogs(args):
    return load_catalogs(args.config)


def _config(args):
    return _catalogs(args).named(args.catalog)


def _open(args, create: bool = False):
    config = _config(args)
    try:
        return open_catalog(config, create=create), config
    except CatalogMissing:
        raise Refused(
            f"No catalog at {config.root}. Ingest into it first, or point [catalog] "
            f"root in {args.config} at an existing one."
        ) from None


def _emit(args, record, render) -> None:
    """Render the record for a person, or print it whole for a program."""
    if not args.json:
        for line in render(record):
            print(line)
        return
    print(json.dumps(_jsonable(record), indent=2, sort_keys=True, default=str))


def _jsonable(record):
    if hasattr(record, "model_dump"):
        return record.model_dump(mode="json")
    if isinstance(record, list):
        return [_jsonable(item) for item in record]
    return record


def _ticker(label: str):
    """A one-line progress display on stderr, for a terminal only."""
    if not sys.stderr.isatty():
        return lambda *a, **k: None

    def show(text: str) -> None:
        print(f"\r{label} {text}", end="", file=sys.stderr, flush=True)

    return show


# ----------------------------------------------------------------------


def _list(args) -> int:
    from .admin import catalogs

    entries = catalogs(_catalogs(args))

    def render(entries):
        for e in entries:
            identity = e.identity or (f"error: {e.error}" if e.error else "not created yet")
            yield f"{e.name}{' (default)' if e.default else ''}"
            yield f"  index     {e.index}"
            yield f"  blobs     {e.blobs}"
            yield f"  identity  {identity}"

    _emit(args, entries, render)
    return 0


def _types(args) -> int:
    from .admin import types

    def render(entries):
        for e in entries:
            files = ", ".join(f".{x}" for x in e.extensions) or "-"
            groups = "  (groups)" if e.groups else ""
            yield f"{e.name:<12} {e.media:<8} {e.subtype:<8} {files}{groups}"
        yield "Read from what is installed. A plugin registers here too; built-ins are reserved."

    _emit(args, types(), render)
    return 0


def _preparers(args) -> int:
    from .admin import preparers

    def render(entries):
        if not entries:
            yield (
                "None installed. A conversion is a plugin — it carries a decoder or a "
                "parser, and a checkout with nothing to convert should not have to install one."
            )
            return
        for e in entries:
            yield f"{e.name:<12} reads {', '.join('.' + x for x in e.reads) or '-'} -> {e.produces}"

    _emit(args, preparers(), render)
    return 0


def _stats(args) -> int:
    from .admin import stats, where_index

    catalog, config = _open(args)
    record = stats(catalog, where_index(config))

    def render(s):
        yield f"{s.where}: {s.samples:,} sample(s)"
        if s.group_sizes:
            g = s.group_sizes
            yield f"  {s.groups:,} group(s), {s.ungrouped:,} sample(s) in no group"
            yield (
                f"    {g.smallest}–{g.largest} samples per group (median {g.median}), "
                f"largest is {g.largest_share:.1%} of the catalog"
            )
            if g.singletons:
                yield f"    {g.singletons:,} group(s) hold a single sample"
        elif s.samples:
            # No grouping is the answer worth noticing: for video frames it
            # means near-duplicates split individually
            yield "  no grouping — right for standalone images, wrong for video frames"
        if s.collections:
            yield "collections"
            for name, count in s.collections.items():
                yield f"  {name:<40} {count:>9,}"
        if s.uncollected:
            yield f"  {s.uncollected:,} sample(s) in no collection — no project draws from them"
        if not s.label_sets:
            yield "No label sets yet."
        for ls in s.label_sets:
            choice = "multi" if ls.multiple else "single"
            detail = "" if ls.multiple is None else f", {choice}-choice"
            yield f"{ls.name} — {ls.task}{detail}"
            yield f"  {ls.annotated:,} annotated, {ls.awaiting:,} awaiting review"
            for class_name, count in ls.classes.items():
                yield f"  {class_name:<30} {count:>9,}"

    _emit(args, record, render)
    return 0


def _probe(args) -> int:
    from .admin import probe

    record = probe(_config(args))

    def render(p):
        yield f"index  {p.index.where}"
        if p.index.reachable:
            yield f"  reachable — {p.index.samples:,} sample(s)"
        else:
            yield f"  unreachable — {p.index.error}"
        if p.blobs is not None:
            yield f"blobs  {p.blobs.where}"
            if p.blobs.ok:
                yield f"  round trip correct — {p.blobs.bytes:,} bytes"
            else:
                yield f"  failed — {p.blobs.error}"
            if p.blobs.note:
                yield f"  {p.blobs.note}"

    _emit(args, record, render)
    return 0 if record.ok else 1


def _copy(args) -> int:
    from .catalog import Catalog
    from .config import blobs_for
    from .sync.copy import CopyError, copy_index

    source, config = _open(args)
    # Only the index moves: the copy points at exactly the same bytes
    target = Catalog.connect(args.to, blobs_for(config))
    show = _ticker("copying")
    try:
        report = copy_index(source, target, on_progress=lambda table, n: show(f"{table} ({n:,})"))
    except CopyError as e:
        raise Refused(str(e)) from None

    def render(_):
        for name, count in report.copied.items():
            yield f"  {name:<18} {count:>9,}"
        yield f"{report.total:,} row(s) copied"
        yield "Blobs were not touched. Point [catalog] url at the new index; leave root as it is."

    _emit(args, {"copied": report.copied, "total": report.total}, render)
    return 0


def _merge(args) -> int:
    from .catalog import Catalog
    from .config import blobs_for
    from .sync.merge import MergeError, merge_annotations

    target, config = _open(args)
    # A copy is the same corpus, so it reads the same bytes this catalog does
    source = Catalog.connect(args.source, blobs_for(config))
    show = _ticker("merging")
    try:
        report = merge_annotations(
            source, target, dry_run=not args.apply, on_progress=lambda d, n: show(f"{d:,}/{n:,}")
        )
    except MergeError as e:
        raise Refused(str(e)) from None

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
            yield f"Nothing to merge from {args.source}."
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


def _repack(args) -> int:
    from .config import blobs_for
    from .storage.blobs import LocalBackend
    from .storage.repack import RepackError, repack_blobs

    config = _config(args)
    if not config.s3_endpoint:
        raise Refused(
            "No object storage configured for this catalog. Set s3_endpoint and s3_bucket in "
            "its [catalog] table, with the credentials in $STRATA_S3_ACCESS_KEY and "
            "$STRATA_S3_SECRET_KEY — this packs blobs into a bucket, so there is nowhere to "
            "put them otherwise."
        )
    catalog, _ = _open(args)
    # This catalog's bucket, not the host default's: with --catalog naming
    # another, the default's would pack one corpus into someone else's.
    target = blobs_for(config)
    target.shard_bytes = args.shard_mb * 1024 * 1024
    # Explicitly the local one: blobs_for answers with the bucket once an
    # endpoint is set, and that is the destination, not the source.
    source = LocalBackend(Path(config.root) / "blobs")
    show = _ticker("packing")
    try:
        report = repack_blobs(
            catalog,
            target,
            source=source,
            dry_run=args.dry_run,
            verify=args.verify,
            on_progress=lambda r: show(
                f"{r.samples:,} sample(s), {r.bytes / 1e9:.1f} GB, {len(r.shards)} shard(s)"
            ),
        )
    except RepackError as e:
        raise Refused(str(e)) from None

    payload = {
        "samples": report.samples,
        "bytes": report.bytes,
        "shards": report.shards,
        "already_packed": report.already_packed,
        "verified": report.verified,
        "failures": report.failures,
        "dry_run": args.dry_run,
    }

    def render(_):
        yield f"from  {source.root}"
        yield f"to    {config.s3_endpoint} bucket={config.s3_bucket} shards={args.shard_mb} MB"
        if args.dry_run:
            shards = -(-report.bytes // target.shard_bytes) if report.bytes else 0
            yield (
                f"  {report.samples:,} sample(s) to pack, {report.bytes / 1e9:.1f} GB, "
                f"about {shards} shard(s)"
            )
            if report.already_packed:
                yield f"  {report.already_packed:,} already packed"
            yield "Nothing was written."
            return
        yield (
            f"{report.samples:,} sample(s) packed into {len(report.shards)} shard(s), "
            f"{report.bytes / 1e9:.1f} GB"
        )
        if report.already_packed:
            yield f"  {report.already_packed:,} were already packed"
        if report.failures:
            yield f"{len(report.failures)} of {report.verified} verified member(s) read back wrong"
            for line in report.failures[:5]:
                yield f"  {line}"
            yield (
                "The index now points at these shards. Local blobs are untouched, so "
                "reverting means restoring location/offset/length from a backup of the index."
            )
            return
        yield f"  {report.verified} member(s) verified"
        yield "Set [catalog] s3_endpoint and s3_bucket in config.toml now."
        yield (
            "Not optional: the index points at shards, and the local backend would look "
            "for one as a file and not find it. Keep the local blobs even so."
        )

    _emit(args, payload, render)
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
