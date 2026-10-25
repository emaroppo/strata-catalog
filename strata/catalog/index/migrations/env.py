"""Alembic environment for the catalog index.

The URL comes from ``config.toml`` (``-x catalog=<name>`` picks a catalog) or from
``$STRATA_CATALOG_URL`` / ``$STRATA_CATALOG_ROOT`` directly. See ``docs/adr/0018``,
``docs/adr/0019``.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context

from strata.catalog.config import CONFIG_ENV, CatalogConfigError, load_catalogs, settings_path
from strata.catalog.index.tables import metadata
from strata.common.migrations import run_alembic


def database_url() -> str:
    url = os.environ.get("STRATA_CATALOG_URL")
    if url:
        return url
    root = os.environ.get("STRATA_CATALOG_ROOT")
    if root:
        return f"sqlite:///{Path(root) / 'catalog.db'}"

    try:
        path = settings_path()
    except CatalogConfigError as e:
        # $STRATA_CONFIG names a file that is not there
        raise SystemExit(str(e)) from None
    if not path.exists():
        raise SystemExit(
            f"No catalog to migrate: there is no {path}. Run from the directory "
            f"holding config.toml, set ${CONFIG_ENV} to it, or set "
            f"STRATA_CATALOG_URL to the index."
        )
    name = context.get_x_argument(as_dictionary=True).get("catalog", "")
    try:
        config = load_catalogs(path).named(name)
    except CatalogConfigError as e:
        raise SystemExit(str(e)) from None
    return config.url or f"sqlite:///{Path(config.root) / 'catalog.db'}"


run_alembic(context, metadata, database_url())
