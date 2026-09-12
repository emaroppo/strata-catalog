"""Getting files for samples that are in no dataset.

Ranking a review pool means scoring every unlabelled sample, and those are
by definition in no dataset version — so the dataset path cannot serve
them. What matters is that this fetches only what is absent, and that a
half-written file can never be mistaken for a whole one.
"""

import hashlib

import pytest

from strata.catalog import blob_path


class Refuses:
    """Has the bytes, will not part with them."""

    def __init__(self, real=None):
        self.real = real
        self.fetched = 0

    def fetch(self, locations):
        locations = list(locations)
        if self.real is None:
            raise AssertionError(f"fetched {len(locations)} blob(s) already on disk")
        self.fetched += len(locations)
        yield from self.real.fetch(locations)

    def get(self, location):
        raise AssertionError("fetched a blob that should have been cached")

    def flush(self):
        return None


@pytest.fixture
def stocked(catalog, files):
    paths = files(4)
    catalog.ingest(paths, media="image", metadata_for=lambda p: {"source_path": str(p)})
    with catalog.engine.connect() as conn:
        from sqlalchemy import select

        from strata.catalog import tables as t

        checksums = [r.checksum for r in conn.execute(select(t.sample.c.checksum))]
    return catalog, checksums


def test_it_returns_a_path_per_sample(stocked, tmp_path):
    catalog, checksums = stocked
    paths = catalog.ensure_cached(checksums, catalog.blobs.root)

    assert set(paths) == set(checksums)
    for checksum, path in paths.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == checksum


def test_what_is_cached_is_not_fetched(stocked, tmp_path):
    catalog, checksums = stocked
    cache = catalog.blobs.root
    catalog.blobs = Refuses()
    # The blob directory the local backend wrote is already the cache layout
    assert len(catalog.ensure_cached(checksums, cache)) == 4


def test_a_miss_is_fetched_once(stocked, tmp_path):
    catalog, checksums = stocked
    cache = tmp_path / "cache"
    catalog.blobs = Refuses(catalog.blobs)

    catalog.ensure_cached(checksums, cache)
    assert catalog.blobs.fetched == 4

    for checksum in checksums:
        assert any(cache.rglob(f"{checksum}*"))

    # And a second call, having filled the cache, fetches nothing
    catalog.blobs = Refuses()
    assert len(catalog.ensure_cached(checksums, cache)) == 4


def test_an_unknown_checksum_is_absent_rather_than_an_error(stocked, tmp_path):
    catalog, checksums = stocked
    paths = catalog.ensure_cached([*checksums, "f" * 64], catalog.blobs.root)
    # The caller asked about samples and is entitled to hear one is not among
    # them, rather than losing the answer for the other four
    assert "f" * 64 not in paths
    assert len(paths) == 4


def test_nothing_half_written_is_left_behind(stocked, tmp_path):
    catalog, checksums = stocked
    cache = tmp_path / "cache"
    catalog.blobs = Refuses(catalog.blobs)
    catalog.ensure_cached(checksums, cache)
    # A short file at the address of a whole one is served as a hit forever,
    # and nothing rehashes a cache entry to notice
    assert list(cache.rglob("*.partial")) == []


def test_the_path_is_where_the_cache_says(stocked, tmp_path):
    catalog, checksums = stocked
    paths = catalog.ensure_cached(checksums[:1], catalog.blobs.root)
    checksum, path = next(iter(paths.items()))
    assert path == catalog.blobs.root / blob_path(checksum, path.suffix)


def test_progress_reaches_the_end(stocked, tmp_path):
    catalog, checksums = stocked
    seen = []
    catalog.ensure_cached(
        checksums, tmp_path / "cache", on_progress=lambda d, t: seen.append((d, t))
    )
    assert seen[-1] == (4, 4)
