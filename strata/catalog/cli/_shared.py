"""What every command starts from: the config, the catalog, the rendering. See ``docs/adr/0030``."""

import sys

from strata.common.cli import emit
from strata.common.errors import Refused

from ..config import CatalogMissing, load_catalogs, open_catalog, settings_path


def _catalogs(args):
    # Resolved in place, so a message that names the file names this one
    args.config = settings_path(args.config)
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
    emit(record, render, as_json=args.json)


def _ticker(label: str):
    """A one-line progress display on stderr, for a terminal only."""
    if not sys.stderr.isatty():
        return lambda *a, **k: None

    def show(text: str) -> None:
        print(f"\r{label} {text}", end="", file=sys.stderr, flush=True)

    return show
