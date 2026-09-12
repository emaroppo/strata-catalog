"""Canonical form, and what a prepared corpus carries beside its files.

The failure both halves guard against is the same one, and it is silent: a
document whose bytes differ from what was annotated. Two files that read
identically ingesting as two samples, or a document whose line endings the
reviewer's browser normalised and the tokenizer did not — either way the
offsets that come back address characters that are not there.
"""

import hashlib

import pytest

from strata.catalog import CatalogError, PreparedIndex, PreparedSample
from strata.catalog.builtin_types import Frames, Image, Text
from strata.catalog.sample_types import SampleTypeError

# ----------------------------------------------------------------------
# Canonical form
# ----------------------------------------------------------------------


def test_only_a_type_with_a_canonical_form_says_so():
    # What lets ingest keep hardlinking a corpus of images without reading
    # a single one of them
    assert Text.canonicalises()
    assert not Image.canonicalises()


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"a\r\nb", b"a\nb"),
        (b"a\rb", b"a\nb"),
        (b"\xef\xbb\xbfhello", b"hello"),
        # NFD e-acute becomes the composed character
        ("café".encode(), "café".encode()),
        (b"already\ncanonical", b"already\ncanonical"),
    ],
)
def test_text_is_stored_one_way(raw, expected):
    assert Text().canonicalise(raw) == expected


def test_canonicalising_twice_changes_nothing():
    # Idempotence is what makes it safe at ingest: a corpus re-ingested
    # after an upgrade has to keep its checksums
    once = Text().canonicalise(b"a\r\nb\xef\xbb\xbf")
    assert Text().canonicalise(once) == once


def test_an_encoding_is_refused_rather_than_guessed():
    # Mojibake ingests, displays as something plausible, and every offset
    # annotated against it is wrong. A refusal is the kinder failure.
    with pytest.raises(SampleTypeError, match="UTF-8"):
        Text().canonicalise("café".encode("latin-1"))


# ----------------------------------------------------------------------
# What ingest does with it
# ----------------------------------------------------------------------


@pytest.fixture
def documents(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    (root / "crlf.txt").write_bytes(b"Dear Bob,\r\n\r\nRegards\r\n")
    (root / "clean.txt").write_bytes(b"Dear Bob,\n\nRegards\n")
    return root


def _stored(catalog, data: bytes):
    """The sample addressed by these bytes, which is what a checksum is."""
    return catalog.samples.by_checksum(hashlib.sha256(data).hexdigest())


def test_a_document_is_stored_in_canonical_form(catalog, documents):
    text = Text()
    catalog.ingest(
        [documents / "crlf.txt"], media="text", canonicalise=text.canonicalise
    )
    # Addressed by the canonical bytes, not by the file's
    sample = _stored(catalog, b"Dear Bob,\n\nRegards\n")
    assert sample is not None
    assert catalog.blobs.get(sample.location) == b"Dear Bob,\n\nRegards\n"


def test_two_documents_that_read_alike_are_one_sample(catalog, documents):
    text = Text()
    crlf = catalog.ingest(
        [documents / "crlf.txt"], media="text", canonicalise=text.canonicalise
    )
    clean = catalog.ingest(
        [documents / "clean.txt"], media="text", canonicalise=text.canonicalise
    )
    # The whole point of an honest checksum: the line endings were never a
    # difference between these two documents
    assert crlf == clean


def test_a_rewritten_sample_says_where_it_came_from(catalog, documents):
    text = Text()
    catalog.ingest(
        [documents / "crlf.txt"], media="text", canonicalise=text.canonicalise
    )
    metadata = _stored(catalog, b"Dear Bob,\n\nRegards\n").metadata
    assert metadata["canonicalised"] is True
    # Traceable back to the file on disk, whose bytes are not the ones stored
    assert len(metadata["source_checksum"]) == 64


def test_a_document_already_canonical_is_not_marked(catalog, documents):
    text = Text()
    catalog.ingest(
        [documents / "clean.txt"], media="text", canonicalise=text.canonicalise
    )
    stored = _stored(catalog, b"Dear Bob,\n\nRegards\n")
    assert "canonicalised" not in (stored.metadata or {})


def test_ingest_names_the_file_it_could_not_store(catalog, tmp_path):
    bad = tmp_path / "latin.txt"
    bad.write_bytes("café".encode("latin-1"))
    with pytest.raises(CatalogError, match="latin.txt"):
        catalog.ingest([bad], media="text", canonicalise=Text().canonicalise)


def test_without_the_hook_bytes_are_stored_as_they_are(catalog, documents):
    # Ingest has always done this, and a media with no canonical form has
    # to keep working exactly as it did
    catalog.ingest([documents / "crlf.txt"], media="text")
    sample = _stored(catalog, b"Dear Bob,\r\n\r\nRegards\r\n")
    assert sample is not None
    assert catalog.blobs.get(sample.location) == b"Dear Bob,\r\n\r\nRegards\r\n"


# ----------------------------------------------------------------------
# The prepared index
# ----------------------------------------------------------------------


def test_an_index_round_trips(tmp_path):
    index = PreparedIndex(
        produced_by="eml",
        samples={"a.txt": PreparedSample(metadata={"from": "bob"}, group_id="thread-1")},
    )
    index.save(tmp_path)
    back = PreparedIndex.load(tmp_path)
    assert back.samples["a.txt"].metadata == {"from": "bob"}
    assert back.samples["a.txt"].group_id == "thread-1"


def test_a_corpus_nobody_prepared_has_no_index(tmp_path):
    assert PreparedIndex.load(tmp_path) is None


def test_merging_keeps_what_the_first_run_wrote(tmp_path):
    first = PreparedIndex(samples={"a.txt": PreparedSample(group_id="one")})
    second = PreparedIndex(samples={"b.txt": PreparedSample(group_id="two")})
    merged = first.merge(second)
    # Converting a source directory that grew adds to the corpus rather
    # than forgetting the rest of it
    assert set(merged.samples) == {"a.txt", "b.txt"}


def test_a_type_reads_what_a_conversion_recorded(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "msg.txt").write_text("hello")
    PreparedIndex(
        samples={"msg.txt": PreparedSample(metadata={"from": "bob"}, group_id="t1")}
    ).save(root)

    text = Text()
    # A type gets prepared metadata and prepared grouping without knowing a
    # converter exists
    assert text.metadata_for(root / "msg.txt", root) == {"from": "bob"}
    assert text.group_id_for(root / "msg.txt", root) == "t1"


def test_a_type_asks_nothing_of_a_corpus_nobody_prepared(tmp_path):
    (tmp_path / "msg.txt").write_text("hello")
    assert Text().metadata_for(tmp_path / "msg.txt", tmp_path) == {}
    assert Text().group_id_for(tmp_path / "msg.txt", tmp_path) is None


def test_frames_prefer_what_extracted_them_to_the_directory(tmp_path):
    root = tmp_path / "corpus"
    (root / "somewhere").mkdir(parents=True)
    frame = root / "somewhere" / "0001.jpg"
    frame.write_bytes(b"x")
    PreparedIndex(samples={"somewhere/0001.jpg": PreparedSample(group_id="video-7")}).save(
        root
    )
    # What extracted the frames knows which video they came from; the
    # directory is how a corpus nobody prepared says the same thing
    assert Frames().group_id_for(frame, root) == "video-7"


def test_frames_fall_back_to_the_directory(tmp_path):
    root = tmp_path / "corpus"
    (root / "video-3").mkdir(parents=True)
    frame = root / "video-3" / "0001.jpg"
    frame.write_bytes(b"x")
    assert Frames().group_id_for(frame, root) == "video-3"


def test_a_corrupt_index_does_not_stop_an_ingest(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "msg.txt").write_text("hello")
    (root / "prepared.json").write_text("{ this is not json")
    # The files are what is being ingested; the index only adds to what is
    # recorded about them
    assert Text().metadata_for(root / "msg.txt", root) == {}


def test_a_reprepared_corpus_is_not_read_from_a_stale_parse(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "msg.txt").write_text("hello")
    PreparedIndex(samples={"msg.txt": PreparedSample(group_id="first")}).save(root)
    assert Text().group_id_for(root / "msg.txt", root) == "first"

    PreparedIndex(samples={"msg.txt": PreparedSample(group_id="second")}).save(root)
    assert Text().group_id_for(root / "msg.txt", root) == "second"
