"""Which catalog a thing belongs to.

A dataset name, a collection, a sample id — every one of them means
something within a catalog, and until now nothing said which. Two hosts
reading "demo" and getting different data is the ambiguity this removes,
and it is the thing a laptop working offline needs before its answers can
be merged back.
"""

from strata.catalog import Catalog, copy_index


def test_an_identity_is_minted_once_and_kept(tmp_path):
    catalog = Catalog.local(tmp_path / "one")
    first = catalog.id
    assert first
    assert catalog.id == first
    # A new connection to the same database is the same catalog
    assert Catalog.local(tmp_path / "one").id == first


def test_two_catalogs_are_different(tmp_path):
    # Unique without coordination, so two machines can each make one offline
    assert Catalog.local(tmp_path / "one").id != Catalog.local(tmp_path / "two").id


def test_an_identity_starts_with_a_sortable_timestamp(tmp_path):
    from datetime import UTC, datetime

    stamp, _, suffix = Catalog.local(tmp_path / "one").id.partition("-")
    # Timestamp first, so merging two catalogs has an order to work with
    # without asking either when it was made
    assert datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    assert len(suffix) == 8


def test_a_copy_is_the_same_catalog(tmp_path):
    source = Catalog.local(tmp_path / "source")
    root = tmp_path / "raw"
    root.mkdir()
    sample = root / "a.jpg"
    sample.write_bytes(b"bytes")
    source.ingest([sample], media="image")

    target = Catalog.local(tmp_path / "target")
    copy_index(source, target)

    # Sample ids are preserved because annotations and task maps reference
    # them, so a copy is the same corpus on another database — and its
    # identity has to say so, or a laptop's offline answers belong nowhere
    assert target.id == source.id
