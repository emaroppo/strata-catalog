"""What every command starts from: the config, the catalog, the rendering.

Standard library only. Every command builds a request, calls the
operation and renders the record; ``--json`` prints the record whole. See
``docs/adr/0030``.
"""

import json
import sys
from pathlib import Path

from ..config import CatalogMissing, load_catalogs, open_catalog

CONFIG = Path("config.toml")


class Refused(Exception):
    """A reason the command stopped, for the user rather than a traceback."""


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
