"""Alembic environment for the catalog index.

The URL is not in ``alembic.ini``. ``$STRATA_CATALOG_URL`` is the same
variable the rest of strata honours as an override, and
``$STRATA_CATALOG_ROOT`` names a local catalog directory for the SQLite
case — the two together cover every configuration without this package
learning to read ``config.toml``, which belongs to the labeller and which
``strata.catalog`` may not import.
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
    raise SystemExit(
        "No catalog to migrate. Set STRATA_CATALOG_URL to the index, or "
        "STRATA_CATALOG_ROOT to a local catalog directory."
    )


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
