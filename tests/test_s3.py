"""Blobs as tar shards in object storage.

No bucket involved: the backend takes a client, and a dict-backed stand-in
implements the two calls it makes. That keeps boto3 optional and the tests
fast, and it exercises the range arithmetic — which is the part that would
silently return the wrong bytes rather than fail.
"""

import tarfile

import pytest

from strata.catalog import checksum_of
from strata.catalog.s3 import S3Backend


class FakeStore:
    """The two S3 calls the backend makes, over a dict."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.ranges: list[str] = []

    def put_object(self, Bucket: str, Key: str, Body: bytes):
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket: str, Key: str, Range: str = ""):
        body = self.objects[Key]
        if Range:
            self.ranges.append(Range)
            first, last = Range.removeprefix("bytes=").split("-")
            body = body[int(first) : int(last) + 1]
        return {"Body": _Reader(body)}


class _Reader:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def backend(store) -> S3Backend:
    return S3Backend(store, bucket="test", shard_bytes=1 << 20)


def a_file(tmp_path, name: str, body: bytes):
    path = tmp_path / name
    path.write_bytes(body)
    return path


# ----------------------------------------------------------------------
# Writing
# ----------------------------------------------------------------------


def test_nothing_is_uploaded_until_flush(backend, store, tmp_path):
    path = a_file(tmp_path, "a.jpg", b"contents")
    backend.put(path, checksum_of(path))
    # A member's offset is known the moment it is written, but the object
    # does not exist until the pack is closed
    assert store.objects == {}

    backend.flush()
    assert len(store.objects) == 1


def test_a_flushed_shard_is_a_readable_tar(backend, store, tmp_path):
    path = a_file(tmp_path, "a.jpg", b"contents")
    backend.put(path, checksum_of(path))
    key = backend.flush()

    with tarfile.open(fileobj=__import__("io").BytesIO(store.objects[key])) as tar:
        assert len(tar.getmembers()) == 1


def test_the_member_is_named_by_checksum(backend, store, tmp_path):
    path = a_file(tmp_path, "a.jpg", b"contents")
    checksum = checksum_of(path)
    backend.put(path, checksum)
    key = backend.flush()

    with tarfile.open(fileobj=__import__("io").BytesIO(store.objects[key])) as tar:
        # A shard is self-describing: the member says which sample it is
        assert tar.getnames() == [f"{checksum[:2]}/{checksum[2:4]}/{checksum}.jpg"]


def test_a_location_names_the_shard_and_the_extent(backend, tmp_path):
    path = a_file(tmp_path, "a.jpg", b"0123456789")
    location = backend.put(path, checksum_of(path))
    assert location.container.endswith(".tar")
    assert location.length == 10
    # Past the tar header, not at the start of the object
    assert location.offset > 0


def test_a_shard_rolls_over_at_the_size_limit(store, tmp_path):
    small = S3Backend(store, bucket="test", shard_bytes=2048)
    for i in range(6):
        path = a_file(tmp_path, f"{i}.bin", bytes(600))
        small.put(path, checksum_of(path))
    small.flush()
    # More than one shard, or the limit meant nothing
    assert len(store.objects) > 1


def test_samples_are_packed_in_the_order_given(backend, store, tmp_path):
    # The caller ingests a group at a time, so a video's frames land
    # together without this having to know what a group is
    checksums = []
    for i in range(4):
        path = a_file(tmp_path, f"{i}.jpg", f"frame {i}".encode())
        checksums.append(checksum_of(path))
        backend.put(path, checksums[-1])
    key = backend.flush()

    with tarfile.open(fileobj=__import__("io").BytesIO(store.objects[key])) as tar:
        assert [n.split("/")[-1].split(".")[0] for n in tar.getnames()] == checksums


def test_flushing_nothing_is_harmless(backend):
    assert backend.flush() is None


# ----------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------


def test_a_sample_comes_back_byte_for_byte(backend, tmp_path):
    path = a_file(tmp_path, "a.jpg", b"the exact bytes")
    location = backend.put(path, checksum_of(path))
    backend.flush()
    assert backend.get(location) == b"the exact bytes"


def test_one_sample_is_one_range_request(backend, store, tmp_path):
    path = a_file(tmp_path, "a.jpg", b"payload")
    location = backend.put(path, checksum_of(path))
    backend.flush()
    backend.get(location)
    # The whole point of recording an offset: a sample is a range, not an
    # object
    assert store.ranges == [f"bytes={location.offset}-{location.offset + 6}"]


def test_every_sample_in_a_shard_reads_back_correctly(backend, tmp_path):
    # The arithmetic that would silently return a neighbour's bytes rather
    # than fail
    expected = {}
    for i in range(12):
        body = f"sample number {i}".encode() * (i + 1)
        path = a_file(tmp_path, f"{i}.jpg", body)
        expected[backend.put(path, checksum_of(path))] = body
    backend.flush()

    for location, body in expected.items():
        assert backend.get(location) == body


def test_fetch_reads_a_shard_whole_rather_than_member_by_member(backend, store, tmp_path):
    locations = []
    for i in range(5):
        path = a_file(tmp_path, f"{i}.jpg", f"body {i}".encode())
        locations.append(backend.put(path, checksum_of(path)))
    backend.flush()

    fetched = dict(backend.fetch(locations))
    assert fetched[locations[2]] == b"body 2"
    # One object, no ranges: pulling it once beats a request per member when
    # most of it is wanted
    assert store.ranges == []


def test_fetch_spans_several_shards(store, tmp_path):
    small = S3Backend(store, bucket="test", shard_bytes=1024)
    locations, bodies = [], []
    for i in range(8):
        body = bytes(400) + bytes([i])
        path = a_file(tmp_path, f"{i}.bin", body)
        locations.append(small.put(path, checksum_of(path)))
        bodies.append(body)
    small.flush()

    assert len({loc.container for loc in locations}) > 1
    fetched = dict(small.fetch(locations))
    assert [fetched[loc] for loc in locations] == bodies


def test_an_empty_fetch_yields_nothing(backend):
    assert list(backend.fetch([])) == []


def test_the_backend_satisfies_the_protocol(store):
    from strata.catalog import BlobBackend

    assert isinstance(S3Backend(store, bucket="test"), BlobBackend)


def test_it_has_no_path_for(store):
    # A tar member in a bucket has no path, which is why path_for is not on
    # the protocol
    assert not hasattr(S3Backend(store, bucket="test"), "path_for")


def test_a_location_from_another_shard_reads_from_that_shard(backend, store, tmp_path):
    first = a_file(tmp_path, "a.jpg", b"first shard")
    location_a = backend.put(first, checksum_of(first))
    backend.flush()
    second = a_file(tmp_path, "b.jpg", b"second shard")
    location_b = backend.put(second, checksum_of(second))
    backend.flush()

    assert location_a.container != location_b.container
    assert backend.get(location_a) == b"first shard"
    assert backend.get(location_b) == b"second shard"


def test_an_ingest_failure_leaves_no_rows_naming_a_missing_shard(tmp_path):
    # flush happens inside the transaction, so an upload that fails takes
    # the rows with it rather than leaving them pointing at nothing
    from strata.catalog import Catalog

    class Failing(FakeStore):
        def put_object(self, Bucket, Key, Body):
            raise OSError("bucket unreachable")

    catalog = Catalog.local(tmp_path / "catalog")
    catalog.blobs = S3Backend(Failing(), bucket="test")
    paths = [a_file(tmp_path, f"{i}.jpg", f"image {i}".encode()) for i in range(3)]

    with pytest.raises(OSError):
        catalog.ingest(paths, media="image")

    label_set_id = catalog.create_label_set(
        "x", __import__("strata.labels", fromlist=["C"]).ClassificationSchema()
    )
    assert catalog.unlabelled(label_set_id) == []


def test_materialising_from_a_bucket_pulls_shards_not_members(store, tmp_path):
    """The dense path, which is what fetch exists for.

    Copying sample by sample would be a range request each; a dataset that
    lives in one shard should cost one object.
    """
    from strata.catalog import Catalog, Manifest
    from strata.labels import Choices, ClassificationSchema

    backend = S3Backend(store, bucket="test", shard_bytes=1 << 20)
    catalog = Catalog.connect(f"sqlite:///{tmp_path / 'index.db'}", backend)

    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    bodies = {}
    for group in ("vid1", "vid2"):
        paths = []
        for i in range(5):
            body = f"{group} frame {i}".encode() * 30
            path = a_file(tmp_path, f"{group}_{i}.jpg", body)
            bodies[path.name] = body
            paths.append(path)
        ids = catalog.ingest(
            paths, media="image", subtype="frames", group_id=group,
            metadata_for=lambda p: {"source_path": p.name},
        )
        catalog.annotate_many(label_set_id, [(i, Choices(values=["a"])) for i in ids])

    store.ranges.clear()
    directory = catalog.materialise(
        catalog.create_dataset("d", label_set_id), tmp_path / "out"
    )
    manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())

    assert len(manifest.samples) == 10
    # No ranges at all: the shard was pulled whole
    assert store.ranges == []
    by_id = {r.id: r for r in catalog.labelled(label_set_id)}
    for sample in manifest.samples:
        source = by_id[sample.id].metadata["source_path"]
        assert (directory / sample.path).read_bytes() == bodies[source]


def test_a_materialised_file_is_not_re_fetched(store, tmp_path):
    from strata.catalog import Catalog
    from strata.labels import Choices, ClassificationSchema

    backend = S3Backend(store, bucket="test", shard_bytes=1 << 20)
    catalog = Catalog.connect(f"sqlite:///{tmp_path / 'index.db'}", backend)
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    for group in ("a", "b"):
        ids = catalog.ingest(
            [a_file(tmp_path, f"{group}{i}.jpg", f"{group}{i}".encode() * 40) for i in range(3)],
            media="image", group_id=group,
        )
        catalog.annotate_many(label_set_id, [(i, Choices(values=["a"])) for i in ids])

    dataset_id = catalog.create_dataset("d", label_set_id)
    out = tmp_path / "out"
    catalog.materialise(dataset_id, out)

    calls = len(store.objects)
    store.ranges.clear()
    catalog.materialise(dataset_id, out)
    # Re-materialising a version rewrites the manifest and fetches nothing
    assert store.ranges == []
    assert len(store.objects) == calls


def test_two_samples_in_one_shard_are_told_apart(store, tmp_path):
    """A container was one file when blobs were files.

    A shard holds hundreds, so matching a location by container alone
    returns whichever the database reaches first — the wrong sample, with no
    error to say so.
    """
    from strata.catalog import Catalog

    backend = S3Backend(store, bucket="test", shard_bytes=1 << 20)
    catalog = Catalog.connect(f"sqlite:///{tmp_path / 'index.db'}", backend)
    catalog.ingest(
        [a_file(tmp_path, f"{i}.jpg", f"body {i}".encode() * 50) for i in range(4)],
        media="image",
    )
    label_set_id = catalog.create_label_set(
        "x", __import__("strata.labels", fromlist=["C"]).ClassificationSchema()
    )
    rows = {r.id: r for r in catalog.unlabelled(label_set_id)}
    assert len({r.location.container for r in rows.values()}) == 1

    for sample_id, row in rows.items():
        found = catalog.by_location(row.location.container, row.location.offset)
        assert found.id == sample_id
