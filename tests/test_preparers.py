"""The conversion surface: what it promises, and what it refuses.

A preparer is the only thing in this workspace that invents bytes rather
than carrying them, which makes it the only thing whose mistakes cannot be
recovered from what is stored. Two of these tests are about ambiguity being
refused rather than resolved — a corpus decided by install order is a corpus
nobody chose.
"""

import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest

from strata.catalog.prepared import PreparedIndex, PreparedSample
from strata.catalog.preparer_conformance import PreparerContract
from strata.catalog.preparers import (
    Prepared,
    Preparer,
    PreparerError,
    available,
    for_source,
    resolve,
    run,
)


class Splitter(Preparer):
    """One log file into one document per line. Enough to be a real corpus."""

    name = "splitter"
    produces = "text"
    sources = frozenset({"log"})

    def prepare(self, source: Path, out_dir: Path):
        text = source.read_bytes().decode("utf-8")
        for index, line in enumerate(text.replace("\r\n", "\n").split("\n")):
            if not line.strip():
                continue
            path = out_dir / f"{source.stem}-{index:03d}.txt"
            path.write_bytes(unicodedata.normalize("NFC", line).encode("utf-8"))
            yield Prepared(
                path=path,
                metadata={"line": index, "from": source.name},
                group_id=source.stem,
            )


class Rival(Splitter):
    """Another way to read the same files, which is the point of it."""

    name = "rival"


class NotAPreparer:
    pass


def _entry(name: str, obj, dist: str = "test-plugin"):
    return SimpleNamespace(
        name=name,
        value=f"{getattr(obj, '__module__', '?')}:{getattr(obj, '__name__', '?')}",
        dist=SimpleNamespace(name=dist),
        load=lambda: obj,
    )


@pytest.fixture
def installed(monkeypatch):
    """Pretend a set of preparers is installed."""

    def _install(*entries):
        monkeypatch.setattr(
            "strata.catalog.preparers.entries", lambda: list(entries)
        )

    return _install


@pytest.fixture
def log(tmp_path) -> Path:
    path = tmp_path / "corpus.log"
    path.write_bytes("first line\r\nsecond line\n\n".encode())
    return path


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------


def test_what_is_installed_is_what_is_listed(installed):
    installed(_entry("splitter", Splitter))
    assert available() == {"splitter": f"{__name__}:Splitter"}


def test_an_unknown_name_says_what_is_installed(installed):
    installed(_entry("splitter", Splitter))
    with pytest.raises(PreparerError, match="splitter"):
        resolve("nothing-like-it")


def test_a_name_registered_twice_is_refused(installed):
    installed(
        _entry("splitter", Splitter, dist="one"), _entry("splitter", Rival, dist="two")
    )
    # Two conversions of one corpus produce different bytes, and install
    # order is not how that gets decided
    with pytest.raises(PreparerError, match="more than once"):
        resolve("splitter")


def test_something_that_is_not_a_preparer_is_refused(installed):
    installed(_entry("splitter", NotAPreparer))
    with pytest.raises(PreparerError, match="not a Preparer"):
        resolve("splitter")


def test_a_source_extension_with_a_dot_is_refused():
    with pytest.raises(PreparerError, match="never match"):

        class Wrong(Preparer):
            sources = frozenset({".log"})


# ----------------------------------------------------------------------
# Choosing one
# ----------------------------------------------------------------------


def test_a_preparer_is_chosen_by_what_it_makes_and_what_it_reads(installed, log):
    installed(_entry("splitter", Splitter))
    assert for_source("text", log) is Splitter


def test_two_that_could_both_do_it_are_refused_by_name(installed, log):
    installed(_entry("splitter", Splitter), _entry("rival", Rival))
    with pytest.raises(PreparerError, match="rival, splitter"):
        for_source("text", log)


def test_nothing_that_can_do_it_says_so(installed, tmp_path):
    installed(_entry("splitter", Splitter))
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"x")
    with pytest.raises(PreparerError, match="frames"):
        for_source("frames", video)


# ----------------------------------------------------------------------
# Running one
# ----------------------------------------------------------------------


def test_it_writes_the_corpus_and_the_index(log, tmp_path):
    out = tmp_path / "out"
    index = run(Splitter(), [log], out)

    assert sorted(p.name for p in out.glob("*.txt")) == [
        "corpus-000.txt",
        "corpus-001.txt",
    ]
    assert index.produced_by == "splitter"
    assert index.samples["corpus-000.txt"].metadata == {"line": 0, "from": "corpus.log"}
    # Grouping stated as a fact about the data rather than a directory layout
    assert index.samples["corpus-001.txt"].group_id == "corpus"


def test_a_source_it_cannot_read_is_refused_not_skipped(tmp_path):
    other = tmp_path / "elsewhere.mp4"
    other.write_bytes(b"x")
    # A corpus quietly smaller than the directory it came from is the
    # failure ingest already goes out of its way to avoid
    with pytest.raises(PreparerError, match="does not admit"):
        run(Splitter(), [other], tmp_path / "out")


def test_a_second_run_adds_to_the_corpus(log, tmp_path):
    out = tmp_path / "out"
    run(Splitter(), [log], out)

    more = tmp_path / "later.log"
    more.write_text("third line\n")
    index = run(Splitter(), [more], out)

    # More mail arrived; the rest of the corpus is still described
    assert set(index.samples) == {
        "corpus-000.txt",
        "corpus-001.txt",
        "later-000.txt",
    }
    assert PreparedIndex.load(out).samples.keys() == index.samples.keys()


def test_an_index_written_by_hand_survives_a_run(log, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    PreparedIndex(samples={"kept.txt": PreparedSample(group_id="elsewhere")}).save(out)
    index = run(Splitter(), [log], out)
    assert "kept.txt" in index.samples


# ----------------------------------------------------------------------
# The contract itself, run against a preparer
# ----------------------------------------------------------------------


class TestSplitterConforms(PreparerContract):
    """Proof the suite runs, and the example a plugin copies."""

    @pytest.fixture
    def preparer(self):
        return Splitter()

    @pytest.fixture
    def source(self, tmp_path):
        path = tmp_path / "corpus.log"
        path.write_bytes("first line\r\nsecond line\n".encode())
        return path
