"""strata-catalog: looking after a catalog from the command line."""

import argparse
from pathlib import Path

from strata.common.cli import run

from ..rows import CatalogError
from ..storage.repack import RepackError
from ..sync.copy import CopyError
from .answers import _export_answers, _import_answers
from .commit import _commit
from .datasets import _dataset_freeze, _dataset_verify, _lists_ls, _lists_put, _lists_show
from .inspect import _list, _probe, _stats, _types
from .raw import _ingest, _prepare, _raw_ls, _runs, _verify
from .remove import _remove
from .repack import _repack
from .sync import _copy, _merge


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    return run(lambda: args.run(args), CatalogError, CopyError, RepackError)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strata-catalog", description="Look after a catalog: what it holds, and where."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Host settings (default: $STRATA_CONFIG, else ./config.toml)",
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

    commit = commands.add_parser(
        "commit", help="A derive or generate run made elsewhere, from its directory, whole"
    )
    commit.add_argument(
        "directory", type=Path, help="Holding run.json, outputs.jsonl and the output files"
    )
    commit.set_defaults(run=_commit)

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

    dataset = commands.add_parser("dataset", help="Dataset versions").add_subparsers(
        dest="dataset", required=True
    )
    check = dataset.add_parser(
        "verify", help="Run a version's spec again at its watermark, and compare its members"
    )
    check.add_argument("version", metavar="NAME@VERSION", help="Which version: enron-sft@3")
    check.set_defaults(run=_dataset_verify)
    freeze = dataset.add_parser(
        "freeze", help="Freeze a version, or reuse the latest if it froze exactly this"
    )
    freeze.add_argument("name", help="The dataset's name")
    freeze.add_argument("--label-set", required=True, metavar="NAME", help="Whose answers count")
    freeze.add_argument("--spec", type=Path, metavar="FILE", help="A selection spec, TOML")
    freeze.add_argument(
        "--collection", action="append", metavar="PATH", help="Draw from here, without a spec"
    )
    freeze.add_argument("--val-ratio", type=float, default=0.2)
    freeze.add_argument("--holdout-ratio", type=float, default=0.0)
    freeze.add_argument("--seed", type=int, default=42)
    freeze.add_argument("--group-by", metavar="KEY", help="A metadata key kept on one side")
    freeze.add_argument(
        "--no-inherit", action="store_true", help="Draw every side afresh, not the last version's"
    )
    freeze.add_argument(
        "--disagreeing-inputs", choices=("drop", "first"), default="drop",
        help="A fine-tuning sample whose origins disagree on its input: left out, or its first",
    )  # fmt: skip
    freeze.set_defaults(run=_dataset_freeze)

    # Not "list", which lists the catalogs this host is configured for
    lists = commands.add_parser(
        "lists", help="Named, versioned sets of checksums a selection spec can name"
    ).add_subparsers(dest="lists", required=True)
    put = lists.add_parser("put", help="Store a set; the same set again is its version")
    put.add_argument("name", help="The list's name, without a version")
    put.add_argument("checksums", type=Path, help="Checksums, one a line; - reads stdin")
    put.add_argument("--type", metavar="TYPE", help="The sample type every member is: text/email")
    put.set_defaults(run=_lists_put)
    show = lists.add_parser("show", help="A version's checksums, one a line")
    show.add_argument("version", metavar="NAME@VERSION")
    show.set_defaults(run=_lists_show)
    lists.add_parser("ls", help="Every list version").set_defaults(run=_lists_ls)

    remove = commands.add_parser(
        "remove", help="Tombstone samples — bad data — and their annotations with them"
    )
    remove.add_argument(
        "--collection", action="append", metavar="PATH", help="Look in this collection"
    )
    remove.add_argument(
        "--where", action="append", metavar="KEY=VALUE",
        help="Metadata that must match; the value is JSON where it parses, so 2000 is a number",
    )  # fmt: skip
    remove.add_argument(
        "--checksum", action="append", metavar="SHA256",
        help="A sample outright; with --type where several types hold the bytes",
    )  # fmt: skip
    remove.add_argument(
        "--type", action="append", metavar="TYPE", help="Only samples of this type: text/email"
    )
    remove.add_argument("--dry-run", action="store_true", help="Report what would go")
    remove.set_defaults(run=_remove)
    return parser


__all__ = ["main"]
