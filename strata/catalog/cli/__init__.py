"""strata-catalog: looking after a catalog from the command line."""

import argparse
import sys
from pathlib import Path

from ..config import CatalogConfigError
from ._shared import CONFIG, Refused
from .inspect import _list, _preparers, _probe, _stats, _types
from .remove import _remove
from .repack import _repack
from .sync import _copy, _merge


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

    remove = commands.add_parser(
        "remove", help="Tombstone samples — bad data — and their annotations with them"
    )
    remove.add_argument(
        "--collection", action="append", metavar="PATH", help="Look in this collection"
    )
    remove.add_argument(
        "--where", action="append", metavar="KEY=VALUE", help="Metadata that must match"
    )
    remove.add_argument("--checksum", action="append", metavar="SHA256", help="A sample outright")
    remove.add_argument("--dry-run", action="store_true", help="Report what would go")
    remove.set_defaults(run=_remove)
    return parser

__all__ = ["main"]
