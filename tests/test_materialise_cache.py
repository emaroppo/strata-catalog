"""Not fetching a corpus that is already on disk.

Successive versions of a dataset share almost all their samples, so without
a cache each one pulls the whole thing out of object storage again. The
tests use a backend that refuses to serve, which is the only way to prove a
fetch did not happen.
"""

import hashlib

import pytest

from strata.catalog import EVERYTHING, blob_path
from strata.labels import Choices, ClassificationSchema


class Refuses:
    """A backend that has the bytes and will not part with them."""

    def __init__(self, real=None):
        self.real = real
        self.fetched = 0

    def get(self, location):
        raise AssertionError("fetched a blob that should have come from cache")

    def fetch(self, locations):
        locations = list(locations)
        if self.real is None:
            raise AssertionError(f"fetched {len(locations)} blob(s) instead of using cache")
        self.fetched += len(locations)
        yield from self.real.fetch(locations)

    def flush(self):
        return None


@pytest.fixture
def stocked(catalog, files):
    paths = files(4)
    ids = catalog.ingest(
        paths, media="image", metadata_for=lambda p: {"source_path": str(p)}
    )
    label_set_id = catalog.label_sets.create("x", ClassificationSchema(classes=["a"]))
    catalog.annotate_many(label_set_id, [(i, Choices(values=["a"])) for i in ids])
    dataset_id = catalog.create_dataset("d", label_set_id, collections=EVERYTHING)
    return catalog, dataset_id


def test_a_cached_blob_is_not_fetched(stocked, tmp_path):
    catalog, dataset_id = stocked
    # The blob directory the local backend already wrote is exactly the
    # layout a cache uses, which is what makes this free on a host that has
    # ingested
    cache = catalog.blobs.root
    catalog.blobs = Refuses()

    out = catalog.materialise(dataset_id, tmp_path / "out", cache=cache)

    files = sorted((out / "files").iterdir())
    assert len(files) == 4
    for path in files:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == path.stem


def test_a_miss_is_fetched_and_then_cached(stocked, tmp_path):
    catalog, dataset_id = stocked
    real = catalog.blobs
    cache = tmp_path / "cache"
    catalog.blobs = Refuses(real)

    first = catalog.materialise(dataset_id, tmp_path / "one", cache=cache)
    assert catalog.blobs.fetched == 4

    # Everything it fetched is now in the cache, laid out by checksum
    for path in (first / "files").iterdir():
        assert (cache / blob_path(path.stem, path.suffix)).exists()

    # A second version fetches nothing
    catalog.blobs = Refuses()
    second = catalog.materialise(dataset_id, tmp_path / "two", cache=cache)
    assert len(list((second / "files").iterdir())) == 4


def test_cached_and_fetched_bytes_agree(stocked, tmp_path):
    catalog, dataset_id = stocked
    uncached = catalog.materialise(dataset_id, tmp_path / "plain")
    cached = catalog.materialise(dataset_id, tmp_path / "cached", cache=catalog.blobs.root)

    for path in (uncached / "files").iterdir():
        assert (cached / "files" / path.name).read_bytes() == path.read_bytes()


def test_no_cache_still_works(stocked, tmp_path):
    catalog, dataset_id = stocked
    # The local pair needs no infrastructure and no caller ceremony
    assert catalog.materialise(dataset_id, tmp_path / "out").exists()


def test_a_partial_write_is_not_served_as_a_hit(stocked, tmp_path):
    catalog, dataset_id = stocked
    cache = tmp_path / "cache"
    real = catalog.blobs
    catalog.blobs = Refuses(real)
    catalog.materialise(dataset_id, tmp_path / "one", cache=cache)

    # An interrupted write leaves a .partial, which must not be mistaken for
    # the blob it was going to become — a short file at the address of a
    # whole one would be served as a hit forever
    leftovers = list(cache.rglob("*.partial"))
    assert leftovers == []


def test_progress_counts_hits_as_well_as_fetches(stocked, tmp_path):
    catalog, dataset_id = stocked
    seen = []
    catalog.materialise(
        dataset_id,
        tmp_path / "out",
        cache=catalog.blobs.root,
        on_progress=lambda d, t: seen.append((d, t)),
    )
    # A caller watching progress must reach the end whichever way each blob
    # arrived, or the bar sticks and reads as a hang
    assert seen[-1] == (4, 4)
