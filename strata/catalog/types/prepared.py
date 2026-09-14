"""What a prepared corpus carries beside its files.

One index at the corpus root: what a conversion knew about each file that
a filename cannot hold — metadata, including any grouping key such as the
video a frame came from, and any candidate annotation the corpus arrived
with. ``SampleType.metadata_for`` reads it by default, so a type gets it
without knowing a converter exists. See ``docs/adr/0010``.
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field

from strata.labels import AnyValue

#: The index's filename, at the root of the prepared corpus.
PREPARED_NAME = "prepared.json"


class PreparedSample(BaseModel):
    """What a conversion knew about one file it wrote."""

    #: Recorded on the sample at ingest, alongside where it came from. A
    #: grouping — the video frames came from, the thread a message is in —
    #: is a key in here, respected by a version frozen with ``group_by``.
    metadata: dict = Field(default_factory=dict)
    #: The annotation the corpus arrived with, where it arrived with one.
    #:
    #: **A candidate, never ground truth.** Corpora that come labelled come
    #: labelled by a regex or by somebody else's model, and the whole point
    #: of the loop is a human deciding. Nothing here reaches the catalog on
    #: its own: landing these is a separate, deliberate step, and it lands
    #: them under a source of their own so an export cannot mistake them
    #: for something a person said.
    value: AnyValue | None = None


class PreparedIndex(BaseModel):
    """Everything a conversion recorded about the corpus it wrote."""

    version: int = 1
    #: Which preparer wrote this, for tracing a corpus back to the code
    #: that made it. Not authoritative for anything.
    produced_by: str = ""
    #: Keyed by path relative to the corpus root, posix-style, so an index
    #: survives the corpus being moved or read from another machine.
    samples: dict[str, PreparedSample] = Field(default_factory=dict)

    # -- reading --------------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> "PreparedIndex | None":
        """The index at ``root``, or None where a corpus was never prepared."""
        path = Path(root) / PREPARED_NAME
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def entry_for(self, path: Path, root: Path) -> PreparedSample | None:
        return self.samples.get(relative_key(path, root))

    # -- writing --------------------------------------------------------

    def save(self, root: Path) -> Path:
        path = Path(root) / PREPARED_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        # Sorted and indented: this is read by people debugging a corpus,
        # and a stable order keeps a re-run's diff to what actually changed.
        path.write_text(
            json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
        return path

    def merge(self, other: "PreparedIndex") -> "PreparedIndex":
        """This index updated with ``other``'s entries.

        Later wins per file, and files this one knows about are kept. What
        makes re-running a conversion over a grown source directory add to a
        corpus rather than replace it.
        """
        return PreparedIndex(
            version=self.version,
            produced_by=other.produced_by or self.produced_by,
            samples={**self.samples, **other.samples},
        )


def relative_key(path: Path, root: Path) -> str:
    """How a file is named in the index: relative to the root, posix-style.

    Falls back to the bare filename for a path outside the root, which is
    not a lookup that should succeed quietly — an absent entry means no
    prepared metadata, and that is the same answer as never having been
    prepared.
    """
    path, root = Path(path), Path(root)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


#: Parsed indexes, keyed by root and the file's mtime and size. Ingest asks
#: per file over a corpus of tens of thousands, and parsing the index each
#: time turns a walk into a quadratic one. Keyed on mtime and size rather
#: than the path alone so a corpus re-prepared inside one process is not
#: read from a stale parse.
_CACHE: dict[tuple[str, int, int], PreparedIndex | None] = {}


def index_for(root: Path) -> PreparedIndex | None:
    """The index at ``root``, parsed once per version of the file."""
    path = Path(root) / PREPARED_NAME
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    if key not in _CACHE:
        try:
            _CACHE[key] = PreparedIndex.load(root)
        except Exception:
            # A corrupt or half-written index is not a reason to refuse a
            # corpus: the files are what is being ingested, and the index
            # only adds to what is recorded about them.
            _CACHE[key] = None
    return _CACHE[key]


__all__ = [
    "PREPARED_NAME",
    "PreparedIndex",
    "PreparedSample",
    "index_for",
    "relative_key",
]
