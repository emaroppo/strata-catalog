"""Blob storage: the local backend, and the object-store rules it keeps anyway."""

from pathlib import Path

from strata.catalog import LocalBackend, Location, checksum_of


def test_checksum_is_stable_for_the_same_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"identical")
    b.write_bytes(b"identical")
    assert checksum_of(a) == checksum_of(b)


def test_put_returns_a_whole_file_range(tmp_path):
    source = tmp_path / "img.jpg"
    source.write_bytes(b"twelve bytes")
    backend = LocalBackend(tmp_path / "blobs")
    location = backend.put(source, checksum_of(source))

    # A file is a shard of one, which is what keeps the three index columns
    # meaningful before object storage exists
    assert (location.offset, location.length) == (0, 12)


def test_get_returns_what_put_stored(tmp_path):
    source = tmp_path / "img.jpg"
    source.write_bytes(b"the actual bytes")
    backend = LocalBackend(tmp_path / "blobs")
    assert backend.get(backend.put(source, checksum_of(source))) == b"the actual bytes"


def test_put_is_idempotent_on_content(tmp_path):
    first, second = tmp_path / "one.jpg", tmp_path / "two.jpg"
    first.write_bytes(b"same")
    second.write_bytes(b"same")
    backend = LocalBackend(tmp_path / "blobs")

    a = backend.put(first, checksum_of(first))
    b = backend.put(second, checksum_of(second))

    # Two names, one copy: the address is the content
    assert a == b
    assert len(list((tmp_path / "blobs").rglob("*.jpg"))) == 1


def test_layout_fans_out_by_checksum(tmp_path):
    source = tmp_path / "img.jpg"
    source.write_bytes(b"x")
    checksum = checksum_of(source)
    location = LocalBackend(tmp_path / "blobs").put(source, checksum)

    # A flat directory of a million entries is slow to stat
    assert location.container == f"{checksum[:2]}/{checksum[2:4]}/{checksum}.jpg"


def test_nothing_partial_is_left_behind(tmp_path):
    source = tmp_path / "img.jpg"
    source.write_bytes(b"complete")
    backend = LocalBackend(tmp_path / "blobs")
    backend.put(source, checksum_of(source))
    assert not list((tmp_path / "blobs").rglob("*.partial"))


def test_path_for_points_at_the_stored_file(tmp_path):
    source = tmp_path / "img.jpg"
    source.write_bytes(b"served to a browser")
    backend = LocalBackend(tmp_path / "blobs")
    location = backend.put(source, checksum_of(source))

    # Deliberately off the protocol: a tar member in a bucket has no path.
    # It exists so Label Studio can serve off the mount until the sample
    # API arrives.
    assert backend.path_for(location).read_bytes() == b"served to a browser"


def test_fetch_yields_each_location_with_its_bytes(tmp_path):
    backend = LocalBackend(tmp_path / "blobs")
    locations = []
    for i in range(3):
        source = tmp_path / f"{i}.jpg"
        source.write_bytes(f"body {i}".encode())
        locations.append(backend.put(source, checksum_of(source)))

    fetched = dict(backend.fetch(locations))
    assert fetched[locations[1]] == b"body 1"
    assert len(fetched) == 3


def test_get_honours_an_offset(tmp_path):
    # Nothing local produces one, but the read path has to be right before
    # tar members start arriving with real offsets
    blobs = tmp_path / "blobs"
    (blobs / "ab" / "cd").mkdir(parents=True)
    (blobs / "ab" / "cd" / "shard.tar").write_bytes(b"XXXXpayloadYYYY")
    backend = LocalBackend(blobs)
    assert backend.get(Location("ab/cd/shard.tar", offset=4, length=7)) == b"payload"


def test_the_backend_satisfies_the_protocol(tmp_path):
    from strata.catalog import BlobBackend

    assert isinstance(LocalBackend(Path(tmp_path)), BlobBackend)
