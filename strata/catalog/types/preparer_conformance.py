"""An executable specification of the preparer contract.

A conversion is the one place in this workspace where bytes are invented
rather than carried, so it is the one place where a mistake is not
recoverable from what is stored. Written as tests a plugin runs against its
own converter:

.. code-block:: python

    from strata.catalog.types.preparer_conformance import PreparerContract

    class TestMyPreparer(PreparerContract):
        @pytest.fixture
        def preparer(self):
            return MyPreparer()

        @pytest.fixture
        def source(self, tmp_path):
            path = tmp_path / "corpus.eml"
            path.write_bytes(...)
            return path

Two fixtures, and the plugin inherits the suite.

**Determinism is the one that matters.** A corpus that comes out different
on a second run re-checksums, and re-checksumming a corpus somebody has
already annotated does not lose the annotations — it silently detaches them,
leaving two samples where there was one and a reviewer being shown a
document they have already done. Everything else here is a convenience
compared to that.

Importing this pulls in pytest, so it lives behind the ``test`` extra and
should only ever be imported from a test module.
"""

from pathlib import Path

import pytest

from .prepared import PREPARED_NAME, PreparedIndex
from .preparers import Preparer, PreparerError, run
from .sample_types import resolve


def _tree(directory: Path) -> dict[str, bytes]:
    """Every file written, by relative path, minus the index itself."""
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != PREPARED_NAME
    }


class PreparerContract:
    """Subclass this in a plugin's tests and supply the two fixtures."""

    @pytest.fixture
    def preparer(self) -> Preparer:
        raise NotImplementedError("Supply a `preparer` fixture returning your preparer")

    @pytest.fixture
    def source(self, tmp_path) -> Path:
        raise NotImplementedError(
            "Supply a `source` fixture returning a path to a source file your "
            "preparer can actually read"
        )

    @pytest.fixture
    def sample_type(self, preparer):
        """The type this claims to produce, as the catalog resolves it.

        Resolved rather than declared, so a preparer naming a type nothing
        installed provides fails here rather than at the first ingest.
        """
        return resolve(type(preparer).produces)()

    # -- declarations ---------------------------------------------------

    def test_it_is_a_preparer(self, preparer):
        assert isinstance(preparer, Preparer)

    def test_it_declares_a_name(self, preparer):
        # What a project names in [data] preparer
        assert isinstance(type(preparer).name, str) and type(preparer).name

    def test_it_declares_what_it_produces(self, preparer, sample_type):
        assert type(preparer).produces

    def test_it_declares_what_it_reads(self, preparer):
        assert type(preparer).sources

    def test_it_admits_its_own_source(self, preparer, source):
        assert preparer.allows(source)

    def test_it_refuses_what_it_does_not_read(self, preparer, source, tmp_path):
        # A source it cannot read is refused rather than skipped: a corpus
        # that quietly comes out smaller than the directory it came from is
        # the failure ingest already goes out of its way to avoid
        foreign = tmp_path / "elsewhere.unlikely-suffix"
        foreign.write_bytes(b"not for you")
        with pytest.raises(PreparerError):
            run(preparer, [foreign], tmp_path / "out")

    # -- what it writes -------------------------------------------------

    def test_it_writes_something(self, preparer, source, tmp_path):
        out = tmp_path / "out"
        run(preparer, [source], out)
        assert _tree(out), "prepared nothing from a source it admits"

    def test_the_output_is_admitted_by_the_type_it_produces(
        self, preparer, source, sample_type, tmp_path
    ):
        """The contract that makes 'prepare, then ingest' hold.

        A converter writing files its own declared type would skip produces
        a corpus that ingests as empty, and ingest reports that as a corpus
        in the wrong place rather than as a broken converter.
        """
        out = tmp_path / "out"
        run(preparer, [source], out)
        for name in _tree(out):
            assert sample_type.allows(out / name), f"{name} is not a {type(sample_type).__name__}"

    def test_the_output_is_already_canonical(self, preparer, source, sample_type, tmp_path):
        """Canonical by construction, not by ingest rewriting it.

        Ingest would canonicalise it anyway, and then the file on disk and
        the sample in the catalog have different bytes and different
        checksums — so the corpus no longer says what was catalogued.
        """
        out = tmp_path / "out"
        run(preparer, [source], out)
        for name, data in _tree(out).items():
            assert sample_type.canonicalise(data) == data, f"{name} is not canonical"

    def test_it_records_every_file_it_wrote(self, preparer, source, tmp_path):
        out = tmp_path / "out"
        index = run(preparer, [source], out)
        assert set(index.samples) == set(_tree(out))
        # And it survives the round trip to disk, which is what ingest reads
        loaded = PreparedIndex.load(out)
        assert loaded is not None
        assert loaded.samples.keys() == index.samples.keys()

    # -- determinism ----------------------------------------------------

    def test_the_same_source_gives_the_same_bytes(self, preparer, source, tmp_path):
        first, second = tmp_path / "first", tmp_path / "second"
        run(preparer, [source], first)
        run(preparer, [source], second)
        assert _tree(first) == _tree(second)

    def test_a_re_run_changes_nothing(self, preparer, source, tmp_path):
        """Converting a source directory that has grown must not disturb it.

        The re-run is the ordinary case — more mail arrives, another video
        is dropped in — and every file it rewrites identically is a sample
        that keeps its checksum and therefore its annotations.
        """
        out = tmp_path / "out"
        run(preparer, [source], out)
        before = _tree(out)
        run(preparer, [source], out)
        assert _tree(out) == before


__all__ = ["PreparerContract"]
