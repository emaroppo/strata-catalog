"""Canonical form: the one way a catalog stores a type's bytes.

The failure this guards against is silent: a document whose bytes differ
from what was annotated. Two files that read
identically ingesting as two samples, or a document whose line endings the
reviewer's browser normalised and the tokenizer did not — either way the
offsets that come back address characters that are not there.
"""

import hashlib
import re

import pytest

from strata.catalog import CanonicalError, CatalogError, canonical_form
from strata.catalog import canonical as forms
from strata.contracts import Frames, Image, Text

text = forms.text

# ----------------------------------------------------------------------
# Canonical form
# ----------------------------------------------------------------------


def test_only_a_media_with_a_canonical_form_has_one():
    # What lets ingest keep hardlinking a corpus of images without reading
    # a single one of them
    assert canonical_form(Text) is forms.text
    assert canonical_form(Image) is None
    assert canonical_form(Frames) is None


def test_a_subtype_takes_its_media_s_form():
    class Email(Text):
        segment = "email"

    assert canonical_form(Email) is forms.text


class _Entry:
    """An entry point, as far as ``canonical_form`` reads one."""

    def __init__(self, name, target):
        self.name, self.target = name, target

    def load(self):
        return self.target


def test_a_plugin_type_can_register_its_own(monkeypatch):
    def shouting(data: bytes) -> bytes:
        return data.upper()

    monkeypatch.setattr(forms, "entries", lambda: [_Entry("text", shouting)])
    # The form registered for the nearest type in the chain wins over the
    # media's default
    assert canonical_form(Text) is shouting


def test_a_registered_form_that_is_not_a_function_is_refused(monkeypatch):
    monkeypatch.setattr(forms, "entries", lambda: [_Entry("text", "not callable")])
    with pytest.raises(CanonicalError, match="not a function"):
        canonical_form(Text)


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
    assert text(raw) == expected


def test_canonicalising_twice_changes_nothing():
    # Idempotence is what makes it safe at ingest: a corpus re-ingested
    # after an upgrade has to keep its checksums
    once = text(b"a\r\nb\xef\xbb\xbf")
    assert text(once) == once


def test_an_encoding_is_refused_rather_than_guessed():
    # Mojibake ingests, displays as something plausible, and every offset
    # annotated against it is wrong. A refusal is the kinder failure.
    with pytest.raises(CanonicalError, match="UTF-8"):
        text("café".encode("latin-1"))


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
    catalog.ingest([documents / "crlf.txt"], media="text", canonicalise=text)
    # Addressed by the canonical bytes, not by the file's
    sample = _stored(catalog, b"Dear Bob,\n\nRegards\n")
    assert sample is not None
    assert catalog.blobs.get(sample.location) == b"Dear Bob,\n\nRegards\n"


def test_two_documents_that_read_alike_are_one_sample(catalog, documents):
    crlf = catalog.ingest([documents / "crlf.txt"], media="text", canonicalise=text)
    clean = catalog.ingest([documents / "clean.txt"], media="text", canonicalise=text)
    # The whole point of an honest checksum: the line endings were never a
    # difference between these two documents
    assert crlf == clean


def test_a_rewritten_sample_says_where_it_came_from(catalog, documents):
    catalog.ingest([documents / "crlf.txt"], media="text", canonicalise=text)
    metadata = _stored(catalog, b"Dear Bob,\n\nRegards\n").metadata
    assert metadata["canonicalised"] is True
    # Traceable back to the file on disk, whose bytes are not the ones stored
    assert len(metadata["source_checksum"]) == 64


def test_a_document_already_canonical_is_not_marked(catalog, documents):
    catalog.ingest([documents / "clean.txt"], media="text", canonicalise=text)
    stored = _stored(catalog, b"Dear Bob,\n\nRegards\n")
    assert "canonicalised" not in (stored.metadata or {})


def test_ingest_names_the_file_it_could_not_store(catalog, tmp_path):
    bad = tmp_path / "latin.txt"
    bad.write_bytes("café".encode("latin-1"))
    with pytest.raises(CatalogError, match=re.escape("latin.txt")):
        catalog.ingest([bad], media="text", canonicalise=text)


def test_without_the_hook_bytes_are_stored_as_they_are(catalog, documents):
    # Ingest has always done this, and a media with no canonical form has
    # to keep working exactly as it did
    catalog.ingest([documents / "crlf.txt"], media="text")
    sample = _stored(catalog, b"Dear Bob,\r\n\r\nRegards\r\n")
    assert sample is not None
    assert catalog.blobs.get(sample.location) == b"Dear Bob,\r\n\r\nRegards\r\n"
