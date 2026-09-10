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
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

from strata.catalog.tables import metadata

alembic_config = context.config

if alembic_config.config_file_name is not None:
    fileConfig(alembic_config.config_file_name)

target_metadata = metadata


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


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = alembic_config.get_section(alembic_config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite cannot ALTER a column, so alembic rebuilds the table.
            # Harmless on Postgres, and required for anything here beyond
            # adding a nullable column.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
