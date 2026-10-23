"""Shared fixtures.

Everything here runs against SQLite and a directory, so the suite needs no
database, no object store and no network — which is the same property that
keeps the repository runnable by someone who just cloned it.
"""

from pathlib import Path

import pytest

from strata.catalog import Catalog
from strata.contracts import ClassificationSchema


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
    return catalog.label_sets.create(
        "presence", ClassificationSchema(classes=["cat", "dog", "bird"])
    )


class FakeObjectStore:
    """Enough of S3 to pack shards into and read ranges back from."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key, Range=""):
        body = self.objects[Key]
        if Range:
            first, last = Range.removeprefix("bytes=").split("-")
            body = body[int(first) : int(last) + 1]
        return {"Body": _Reader(body)}


class _Reader:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body


@pytest.fixture
def object_store() -> FakeObjectStore:
    return FakeObjectStore()
