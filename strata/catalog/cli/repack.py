"""The repack command: local blobs into tar shards in a bucket."""

from pathlib import Path

from ._shared import Refused, _config, _emit, _open, _ticker


def _repack(args) -> int:
    from ..config import blobs_for
    from ..storage.blobs import LocalBackend
    from ..storage.repack import RepackError, repack_blobs

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
    from ..storage.s3 import S3Backend

    target = blobs_for(config)
    if not isinstance(target, S3Backend):
        raise Refused("Repacking needs [catalog] s3_endpoint: shards go to a bucket.")
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
