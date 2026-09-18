"""Taking a prepared corpus in, or refusing it whole.

A corpus a catalog takes is exactly what its index names, each file
meeting the type the index declares. Anything short of that is refused
before a byte is written, with every shortfall said at once.
"""

import hashlib

import pytest

from strata.catalog import IntakeError, admit
from strata.contracts import (
    PREPARED_FORMAT,
    PREPARED_NAME,
    Frames,
    Image,
    PreparedIndex,
    PreparedSample,
    Spans,
    Text,
)
from strata.contracts.values import Span


def prepare(root, type_name, samples, files=None):
    """Write a corpus: ``samples`` into the index, ``files`` (default: the same) onto disk."""
    root.mkdir(parents=True, exist_ok=True)
    for key, content in (files if files is not None else dict.fromkeys(samples, b"x")).items():
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    index = PreparedIndex(
        version=PREPARED_FORMAT,
        type=type_name,
        samples={
            key: entry if isinstance(entry, PreparedSample) else PreparedSample(metadata=entry)
            for key, entry in samples.items()
        },
    )
    (root / PREPARED_NAME).write_text(index.to_json())
    return root


# ----------------------------------------------------------------------
# What is taken
# ----------------------------------------------------------------------


def test_a_corpus_is_taken_as_its_index_describes_it(tmp_path):
    root = prepare(
        tmp_path / "corpus",
        "frames",
        {"clip-a/000001.jpg": {"video": "clip-a", "frame_index": 1}},
    )
    admission = admit(root, Frames(), "frames")
    assert admission.entries == {
        root / "clip-a/000001.jpg": {
            "video": "clip-a",
            "frame_index": 1,
            "source_path": "clip-a/000001.jpg",
        }
    }
    assert admission.unindexed == 0


def test_where_a_file_sat_is_the_catalog_s_to_record(tmp_path):
    # Whatever a preparer wrote under the key, the path in the corpus is a
    # fact the catalog states itself
    root = prepare(tmp_path / "c", "image", {"a.jpg": {"source_path": "elsewhere"}})
    assert admit(root, Image(), "image").entries[root / "a.jpg"]["source_path"] == "a.jpg"


def test_a_file_the_index_does_not_name_is_counted_not_taken(tmp_path):
    root = prepare(
        tmp_path / "c",
        "image",
        {"a.jpg": {}},
        files={"a.jpg": b"a", "stray.jpg": b"s", "notes.txt": b"n"},
    )
    admission = admit(root, Image(), "image")
    assert list(admission.entries) == [root / "a.jpg"]
    assert admission.unindexed == 2


def test_candidate_annotations_come_with_their_files(tmp_path):
    spans = Spans(values=[Span(labels=["PER"], start=0, end=3)])
    root = prepare(
        tmp_path / "c",
        "text",
        {"a.txt": PreparedSample(value=spans), "b.txt": PreparedSample()},
        files={"a.txt": b"Bob\n", "b.txt": b"none\n"},
    )
    admission = admit(root, Text(), "text")
    assert admission.values == {root / "a.txt": spans}


# ----------------------------------------------------------------------
# What is refused, whole
# ----------------------------------------------------------------------


def test_a_directory_nobody_prepared_is_refused_with_what_to_do(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "a.jpg").write_bytes(b"x")
    with pytest.raises(IntakeError, match="run a preparer"):
        admit(tmp_path / "raw", Image(), "image")


def test_an_index_from_before_it_named_its_type_is_refused(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    (root / PREPARED_NAME).write_text('{"version": 1, "samples": {}}')
    with pytest.raises(IntakeError, match="Prepare the corpus again"):
        admit(root, Image(), "image")


def test_a_corrupt_index_is_refused_rather_than_ignored(tmp_path):
    # It used to be tolerated, since the files were what was ingested. Now
    # the index is, and a corpus without one cannot be taken
    root = tmp_path / "corrupt"
    root.mkdir()
    (root / PREPARED_NAME).write_text('{"version": 2, "type": "ima')
    with pytest.raises(IntakeError, match=r"can read"):
        admit(root, Image(), "image")


def test_a_corpus_of_another_type_is_refused(tmp_path):
    root = prepare(tmp_path / "c", "image", {"clip/1.jpg": {}})
    with pytest.raises(IntakeError, match="prepared as 'image'"):
        admit(root, Frames(), "frames")


def test_a_frame_with_no_video_refuses_the_corpus_and_names_itself(tmp_path):
    root = prepare(
        tmp_path / "c",
        "frames",
        {"clip/1.jpg": {"video": "clip"}, "clip/2.jpg": {}, "clip/3.jpg": {"video": ""}},
    )
    with pytest.raises(IntakeError) as refused:
        admit(root, Frames(), "frames")
    message = str(refused.value)
    assert "2 of 3 files" in message
    assert "clip/2.jpg" in message and "clip/3.jpg" in message
    assert "nothing was ingested" in message
    assert {v.key for v in refused.value.violations} == {"clip/2.jpg", "clip/3.jpg"}


def test_a_long_refusal_lists_some_and_counts_the_rest(tmp_path):
    root = prepare(tmp_path / "c", "frames", {f"clip/{i:03}.jpg": {} for i in range(30)})
    with pytest.raises(IntakeError, match=r"and 10 more"):
        admit(root, Frames(), "frames")


def test_an_annotation_on_text_the_catalog_would_rewrite_is_refused(tmp_path):
    # CRLF: the catalog stores LF, so offsets counted over these bytes would
    # land a character late for every line above them
    spans = Spans(values=[Span(labels=["PER"], start=5, end=8)])
    root = prepare(
        tmp_path / "c",
        "text",
        {"a.txt": PreparedSample(value=spans)},
        files={"a.txt": b"Hi\r\n\r\nBob\r\n"},
    )
    with pytest.raises(IntakeError, match="has to write it canonical"):
        admit(root, Text(), "text")


def test_text_with_no_annotation_is_left_for_the_catalog_to_canonicalise(tmp_path):
    root = prepare(tmp_path / "c", "text", {"a.txt": {}}, files={"a.txt": b"Hi\r\n"})
    assert root / "a.txt" in admit(root, Text(), "text").entries


def test_a_refused_corpus_writes_nothing(catalog, tmp_path):
    # The one good frame is not taken either: a corpus is taken whole, so a
    # re-run after the fix is the only run that counts
    root = prepare(
        tmp_path / "c",
        "frames",
        {"clip/1.jpg": {"video": "clip"}, "clip/2.jpg": {}},
        files={"clip/1.jpg": b"good", "clip/2.jpg": b"bad"},
    )
    with pytest.raises(IntakeError):
        admission = admit(root, Frames(), "frames")
        catalog.ingest(list(admission.entries), media="image", subtype="frames")
    assert catalog.samples.by_checksum(hashlib.sha256(b"good").hexdigest()) is None
