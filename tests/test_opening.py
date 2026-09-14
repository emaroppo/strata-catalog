"""Opening a catalog consults it; creating one is asked for.

The failure this guards against is quiet: a command that only reads
leaving a fresh, empty catalog behind, or issuing DDL against a live one
before the migration guard has had its say.
"""

import re

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine

from strata.catalog import Catalog, CatalogMissing, LocalBackend
from strata.catalog.config import CatalogConfig, open_catalog

WRITES = re.compile(r"^\s*(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE)\b", re.IGNORECASE)


@pytest.fixture
def statements():
    """Every statement any engine runs while the fixture is live."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", record)
    yield seen
    event.remove(Engine, "before_cursor_execute", record)


def test_opening_an_empty_index_creates_nothing(tmp_path):
    url = f"sqlite:///{tmp_path / 'nothing.db'}"
    with pytest.raises(CatalogMissing, match="No catalog at"):
        Catalog.connect(url, LocalBackend(tmp_path / "blobs"))
    from sqlalchemy import create_engine

    assert inspect(create_engine(url)).get_table_names() == []


def test_opening_an_existing_catalog_writes_nothing(tmp_path, statements):
    root = tmp_path / "catalog"
    made = Catalog.local(root)
    identity = made.id
    statements.clear()

    opened = Catalog.connect(f"sqlite:///{root / 'catalog.db'}", LocalBackend(root / "blobs"))

    assert opened.id == identity
    written = [s for s in statements if WRITES.match(s)]
    assert not written, written


def test_creating_twice_opens_the_second_time(tmp_path):
    root = tmp_path / "catalog"
    first = Catalog.local(root)
    second = Catalog.local(root)
    # Same catalog, not a new identity minted over the old one
    assert second.id == first.id


def test_a_config_is_opened_unless_asked_to_create(tmp_path):
    config = CatalogConfig(root=str(tmp_path / "catalog"))
    with pytest.raises(CatalogMissing):
        open_catalog(config)
    assert not (tmp_path / "catalog").exists()
    created = open_catalog(config, create=True)
    assert open_catalog(config).id == created.id


def test_a_configured_index_with_no_catalog_is_missing_not_made(tmp_path):
    config = CatalogConfig(root=str(tmp_path / "blobs"), url=f"sqlite:///{tmp_path / 'idx.db'}")
    with pytest.raises(CatalogMissing):
        open_catalog(config)
    assert open_catalog(config, create=True).id == open_catalog(config).id
