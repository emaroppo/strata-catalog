"""strata-catalog: looking after a catalog from the command line."""

import argparse
import sys
from pathlib import Path

from ..config import CatalogConfigError
from ._shared import CONFIG, Refused
from .answers import _export_answers, _import_answers
from .inspect import _list, _probe, _stats, _types
from .raw import _ingest, _prepare, _raw_ls, _runs, _verify
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
    commands.add_parser("types", help="The sample types this catalog can take").set_defaults(
        run=_types
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

    ingest = commands.add_parser("ingest", help="Keep raw files as they are, under a collection")
    ingest.add_argument("root", type=Path, help="Directory the names are paths under")
    ingest.add_argument(
        "--collection", required=True, metavar="PATH", help="Where they go: enron/maildir"
    )
    ingest.add_argument(
        "--files", type=Path, metavar="LIST", help="Paths under the root, one a line (default: all)"
    )
    ingest.set_defaults(run=_ingest)

    prepare = commands.add_parser("prepare", help="Prepare samples from raw files, by a spec")
    prepare.add_argument("spec", type=Path, help="The spec: a chain of stages, and the output")
    prepare.add_argument(
        "--files", type=Path, required=True, metavar="LIST",
        help="Raw names or checksums, one a line; - reads stdin",
    )  # fmt: skip
    prepare.add_argument(
        "--allow-missing", action="store_true", help="Count names nothing ingested, not refuse"
    )
    prepare.add_argument(
        "--continues", metavar="RUN", help="The run whose samples this one supersedes"
    )
    prepare.set_defaults(run=_prepare)

    raw = commands.add_parser("raw", help="Raw files").add_subparsers(dest="raw", required=True)
    ls = raw.add_parser("ls", help="A collection's names, one a line; or every collection")
    ls.add_argument("collection", nargs="?", help="Which collection (default: list them)")
    ls.set_defaults(run=_raw_ls)

    export = commands.add_parser(
        "export-answers", help="One label set's answers and their history, by checksum, to a file"
    )
    export.add_argument("--label-set", required=True, metavar="NAME", help="Which label set")
    export.add_argument(
        "--source", action="append", metavar="SOURCE", help="Only answers from this source"
    )
    export.add_argument("--out", type=Path, metavar="FILE", help="Where (default: stdout)")
    export.set_defaults(run=_export_answers)

    imported = commands.add_parser(
        "import-answers", help="Another catalog's exported answers, as an import run"
    )
    imported.add_argument("answers", type=Path, help="A file export-answers wrote")
    imported.add_argument("--into", metavar="NAME", help="The label set here (default: its own)")
    imported.add_argument(
        "--pairs", type=Path, metavar="LIST",
        help="'old new' checksums, one pair a line, for samples whose bytes changed",
    )  # fmt: skip
    imported.add_argument("--dry-run", action="store_true", help="Report, and keep nothing")
    imported.set_defaults(run=_import_answers)

    runs = commands.add_parser("runs", help="What wrote to the catalog")
    runs.add_argument("--kind", help="Only runs of this kind: ingest, prepare, import, ...")
    runs.set_defaults(run=_runs)

    verify = commands.add_parser("verify", help="Check a run is still what it recorded")
    verify.add_argument("--run", dest="run_id", required=True, metavar="ID", help="Which run")
    verify.add_argument("--deep", action="store_true", help="Also hash every blob it wrote")
    verify.set_defaults(run=_verify)

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
