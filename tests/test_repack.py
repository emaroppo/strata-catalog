"""Moving blobs into shards without losing where they are.

The failure this guards against is not an exception — it is an index that
reads, returns bytes, and returns the wrong ones. So most of these assert on
content read back through the target rather than on rows.
"""

import hashlib

import pytest
from sqlalchemy import select

from strata.catalog import Catalog, RepackError, repack_blobs
from strata.catalog.index import tables as t
from strata.catalog.storage.s3 import S3Backend


class FakeStore:
    """Enough of S3 to pack against, with real range semantics."""

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
def store():
    return FakeStore()


@pytest.fixture
def stocked(tmp_path):
    """A local catalog holding twelve samples across three groups."""
    catalog = Catalog.local(tmp_path / "catalog")
    root = tmp_path / "raw"
    root.mkdir()
    contents = {}
    for group in range(3):
        paths = []
        for i in range(4):
            path = root / f"g{group}_{i}.jpg"
            # Distinct lengths, so an off-by-one in offsets cannot pass by
            # returning a neighbour of the same size
            path.write_bytes(f"group {group} image {i}".encode() * (i + 1))
            contents[path.name] = path.read_bytes()
            paths.append(path)
        catalog.ingest(paths, media="image", group_id=f"vid{group}")
    return catalog, contents


def locations(catalog):
    with catalog.engine.connect() as conn:
        return conn.execute(
            select(t.sample.c.checksum, t.sample.c.location, t.sample.c.offset,
                   t.sample.c.length)
        ).all()


# ----------------------------------------------------------------------
# The move itself
# ----------------------------------------------------------------------


def test_every_sample_reads_back_as_itself(stocked, store):
    catalog, _ = stocked
    target = S3Backend(store, bucket="b")

    repack_blobs(catalog, target)

    # The whole point: bytes addressed through the new locations still hash
    # to what the index says they are
    for row in locations(catalog):
        from strata.catalog import Location

        body = target.get(Location(row.location, row.offset, row.length))
        assert hashlib.sha256(body).hexdigest() == row.checksum


def test_locations_move_into_the_shard_prefix(stocked, store):
    catalog, _ = stocked
    repack_blobs(catalog, S3Backend(store, bucket="b"))
    assert all(row.location.startswith("shards/") for row in locations(catalog))


def test_the_shard_is_uploaded_before_rows_name_it(stocked, store):
    catalog, _ = stocked
    repack_blobs(catalog, S3Backend(store, bucket="b"))
    # An index naming an object nobody uploaded is the one state no re-run
    # can detect and no read survives
    named = {row.location for row in locations(catalog)}
    assert named <= set(store.objects)


def test_a_group_is_packed_in_one_run(stocked, store):
    catalog, _ = stocked
    # Small enough that groups span shards, which is allowed. What is not
    # allowed is interleaving: a video's frames scattered among another's
    # turns a dense read into a request per frame.
    target = S3Backend(store, bucket="b", shard_bytes=2048)
    report = repack_blobs(catalog, target)
    assert len(report.shards) > 1, "sizing no longer exercises shard rollover"

    order = {key: i for i, key in enumerate(report.shards)}
    with catalog.engine.connect() as conn:
        rows = conn.execute(
            select(t.sample.c.group_id, t.sample.c.location, t.sample.c.offset)
        ).all()
    laid_out = [
        row.group_id
        for row in sorted(rows, key=lambda r: (order[r.location], r.offset))
    ]
    # Each group appears as one unbroken run
    runs = [g for i, g in enumerate(laid_out) if i == 0 or g != laid_out[i - 1]]
    assert len(runs) == len(set(laid_out))


def test_report_counts_what_moved(stocked, store):
    catalog, contents = stocked
    report = repack_blobs(catalog, S3Backend(store, bucket="b"))
    assert report.samples == 12
    assert report.bytes == sum(len(b) for b in contents.values())
    assert report.ok


# ----------------------------------------------------------------------
# Resuming
# ----------------------------------------------------------------------


def test_a_second_run_moves_nothing(stocked, store):
    catalog, _ = stocked
    target = S3Backend(store, bucket="b")
    repack_blobs(catalog, target)
    before = set(store.objects)

    again = repack_blobs(catalog, target)

    assert again.samples == 0
    assert again.already_packed == 12
    # Packing twice would double the bucket and orphan the first copy
    assert set(store.objects) == before


def test_an_interrupted_run_resumes(stocked, store):
    catalog, _ = stocked
    target = S3Backend(store, bucket="b", shard_bytes=1)

    boom = RuntimeError("connection reset")
    real_put = target.put
    calls = {"n": 0}

    def flaky(source, checksum):
        calls["n"] += 1
        if calls["n"] > 5:
            raise boom
        return real_put(source, checksum)

    target.put = flaky
    with pytest.raises(RuntimeError):
        repack_blobs(catalog, target)

    target.put = real_put
    report = repack_blobs(catalog, target, verify=0)

    # Whatever the first run committed stays committed; the rest follows,
    # and no sample is packed twice
    assert report.samples + report.already_packed == 12
    assert all(row.location.startswith("shards/") for row in locations(catalog))


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_a_source_without_files_is_refused(stocked, store):
    catalog, _ = stocked
    target = S3Backend(store, bucket="b")
    with pytest.raises(RepackError, match="no files behind it"):
        repack_blobs(catalog, target, source=target)


def test_a_shard_too_big_for_the_offset_column_is_refused(stocked, store):
    catalog, _ = stocked
    target = S3Backend(store, bucket="b", shard_bytes=2**31)
    # Offsets past 2^31 wrap in an INTEGER column, and the sample recorded
    # at a wrapped offset reads as whatever happens to be there
    with pytest.raises(RepackError, match="offset column"):
        repack_blobs(catalog, target)


# ----------------------------------------------------------------------
# Verification and dry runs
# ----------------------------------------------------------------------


def test_verification_catches_a_backend_ignoring_range(stocked, store):
    catalog, _ = stocked

    class Deaf(FakeStore):
        def get_object(self, Bucket, Key, Range=""):
            # The failure the probe exists for, arriving here instead
            return super().get_object(Bucket, Key)

    target = S3Backend(Deaf(), bucket="b")
    report = repack_blobs(catalog, target, verify=12)
    assert not report.ok
    assert report.failures


def test_a_dry_run_writes_nothing(stocked, store):
    catalog, _ = stocked
    report = repack_blobs(catalog, S3Backend(store, bucket="b"), dry_run=True)

    assert report.samples == 12
    assert report.bytes > 0
    assert store.objects == {}
    assert all(not row.location.startswith("shards/") for row in locations(catalog))
