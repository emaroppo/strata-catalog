"""The migration chain builds the schema the code expects.

Without this, migrations rot: ``create_all`` keeps every test green while
the chain quietly stops describing the same database, and the divergence
surfaces on the one machine that has an existing catalog — which is the
machine with the data on it.

The comparison is alembic's own autogenerate diff. If someone adds a column
to ``tables.py`` and no migration, this fails naming it.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from strata.catalog import tables as t
from strata.catalog.schema_version import MIGRATIONS, script_directory


@pytest.fixture
def url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'catalog.db'}"


def _config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.set_main_option("sqlalchemy.url", url)
    return config


def _upgrade(url: str, monkeypatch) -> None:
    monkeypatch.setenv("STRATA_CATALOG_URL", url)
    command.upgrade(_config(url), "head")


def test_the_chain_produces_the_schema_the_code_expects(url, monkeypatch):
    _upgrade(url, monkeypatch)
    engine = create_engine(url)

    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        difference = compare_metadata(context, t.metadata)

    assert not difference, (
        f"the migration chain and tables.py disagree — add a migration for the change: {difference}"
    )


def test_the_chain_and_create_all_agree_on_the_tables(url, monkeypatch, tmp_path):
    _upgrade(url, monkeypatch)
    migrated = set(inspect(create_engine(url)).get_table_names())

    direct_url = f"sqlite:///{tmp_path / 'direct.db'}"
    direct = create_engine(direct_url)
    t.metadata.create_all(direct)
    built = set(inspect(direct).get_table_names())

    # The chain also leaves alembic's own bookkeeping table behind.
    assert migrated - {"alembic_version"} == built


def test_a_created_catalog_is_stamped_at_head(tmp_path):
    """Otherwise its first upgrade replays the baseline over live tables."""
    from strata.catalog import Catalog

    catalog = Catalog.local(tmp_path / "catalog")

    with catalog.engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()

    assert current == script_directory().get_current_head()


def test_stamping_leaves_a_database_mid_history_alone(url, monkeypatch):
    """A database part-way through the chain is alembic's to move, not ours."""
    from strata.catalog.schema_version import stamp_if_new

    monkeypatch.setenv("STRATA_CATALOG_URL", url)
    base = script_directory().get_base()
    command.stamp(_config(url), base)

    engine = create_engine(url)
    assert stamp_if_new(engine) is None

    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == base


def test_every_migration_has_a_down_path(tmp_path, monkeypatch):
    """A chain that cannot be walked back cannot be rehearsed."""
    _upgrade(f"sqlite:///{tmp_path / 'c.db'}", monkeypatch)
    for script in script_directory().walk_revisions():
        assert Path(script.path).exists()
        assert script.down_revision is not None or script.is_base
