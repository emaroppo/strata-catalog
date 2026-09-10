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


def _head_of(db) -> str | None:
    with create_engine(f"sqlite:///{db}").connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def test_the_catalog_to_migrate_comes_from_config_toml(tmp_path, monkeypatch):
    """Where every other reader gets it, so migrating is not a configuration of its own."""
    for name in ("STRATA_CATALOG_URL", "STRATA_CATALOG_ROOT"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "catalog").mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'[catalog]\nroot = "{tmp_path / "catalog"}"\n')
    monkeypatch.setenv("STRATA_CONFIG", str(config))

    command.upgrade(_config(""), "head")

    assert _head_of(tmp_path / "catalog" / "catalog.db") == script_directory().get_current_head()


def test_a_named_catalog_can_be_migrated(tmp_path, monkeypatch):
    import argparse

    for name in ("STRATA_CATALOG_URL", "STRATA_CATALOG_ROOT"):
        monkeypatch.delenv(name, raising=False)
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[catalog]\ndefault = "a"\n\n[catalog.a]\nroot = "{tmp_path / "a"}"\n\n'
        f'[catalog.b]\nroot = "{tmp_path / "b"}"\n'
    )
    monkeypatch.setenv("STRATA_CONFIG", str(config))
    alembic_config = _config("")
    alembic_config.cmd_opts = argparse.Namespace(x=["catalog=b"])

    command.upgrade(alembic_config, "head")

    assert _head_of(tmp_path / "b" / "catalog.db") == script_directory().get_current_head()
    assert not (tmp_path / "a" / "catalog.db").exists()


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


def test_an_unmigrated_catalog_is_refused_rather_than_mis_stamped(tmp_path, monkeypatch):
    """The dangerous case: tables but no revision.

    `connect` calls `create_all` on every open, so a database that predates
    migrations would otherwise be stamped at head — claiming columns it
    does not have, and leaving a later `upgrade` with nothing to do.
    """
    from strata.catalog import Catalog
    from strata.catalog.schema_version import SchemaOutOfDate

    root = tmp_path / "catalog"
    Catalog.local(root)
    engine = create_engine(f"sqlite:///{root / 'catalog.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM alembic_version")

    with pytest.raises(SchemaOutOfDate) as raised:
        Catalog.local(root)

    assert "stamp" in str(raised.value) and "upgrade head" in str(raised.value)


def test_a_catalog_behind_head_names_the_upgrade(tmp_path):
    from strata.catalog import Catalog
    from strata.catalog.schema_version import SchemaOutOfDate, script_directory

    root = tmp_path / "catalog"
    Catalog.local(root)
    engine = create_engine(f"sqlite:///{root / 'catalog.db'}")
    base = script_directory().get_base()
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM alembic_version")
        conn.exec_driver_sql(f"INSERT INTO alembic_version VALUES ('{base}')")

    with pytest.raises(SchemaOutOfDate, match="upgrade head"):
        Catalog.local(root)
