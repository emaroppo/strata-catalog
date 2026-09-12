"""Alembic environment for the catalog index.

There is no ``alembic.ini``: the URL comes from where every other reader of
a catalog gets it: the ``[catalog]`` tables of ``config.toml`` — the file
``$STRATA_CONFIG`` names, or ``./config.toml`` — and their default, or the
one named with ``-x catalog=<name>``. The password comes from
``$PGPASSWORD``, as it does everywhere.

``$STRATA_CATALOG_URL``, or ``$STRATA_CATALOG_ROOT`` for a SQLite directory,
points a run at one database directly, which is how a test migrates a
scratch one.
"""

from __future__ import annotations

import os
from pathlib import Path

from alembic import context

from strata.catalog.tables import metadata
from strata.common.migrations import run_alembic


def database_url() -> str:
    url = os.environ.get("STRATA_CATALOG_URL")
    if url:
        return url
    root = os.environ.get("STRATA_CATALOG_ROOT")
    if root:
        return f"sqlite:///{Path(root) / 'catalog.db'}"

    from strata.catalog.config import CONFIG_ENV, CatalogConfigError, load_catalogs

    path = Path(os.environ.get(CONFIG_ENV) or "config.toml")
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
