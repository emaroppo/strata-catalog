"""Shared fixtures.

Everything here runs against SQLite and a directory, so the suite needs no
database, no object store and no network — which is the same property that
keeps the repository runnable by someone who just cloned it.
"""

from pathlib import Path

import pytest

from strata.catalog import Catalog
from strata.labels import ClassificationSchema


@pytest.fixture
def catalog(tmp_path) -> Catalog:
    return Catalog.local(tmp_path / "catalog")


@pytest.fixture
def files(tmp_path):
    """Make n files with distinct contents, so checksums differ."""

    def _make(n: int = 10, prefix: str = "img", suffix: str = ".jpg") -> list[Path]:
        root = tmp_path / "raw"
        root.mkdir(exist_ok=True)
        paths = []
        for i in range(n):
            path = root / f"{prefix}{i:03d}{suffix}"
            path.write_bytes(f"contents of {prefix}{i}".encode())
            paths.append(path)
        return paths

    return _make


@pytest.fixture
def label_set(catalog) -> int:
    return catalog.create_label_set(
        "presence", ClassificationSchema(classes=["cat", "dog", "bird"])
    )
