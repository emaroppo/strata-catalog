"""Getting a corpus into the shape a sample type stores.

A sample type says what the catalog holds — which files are admitted, what
canonical form they are in, what is recorded about each one. Almost no
corpus arrives that way. Mail arrives as ``.eml`` or as a blob of message
JSON; frames arrive as video. Something has to turn the second into the
first, and today that is a script beside the repository rather than part of
it.

**Upstream of ingest, and separate from it.** A preparer writes files and an
index; ``ingest`` catalogues them. Keeping the two apart is what stops a
converter becoming a second implementation of content addressing, grouping
and collections — the reason the email import was already two phases with
``ingest`` in the middle.

**A plugin surface, like the other three.** Entry point group, registry, a
``resolve()`` that refuses ambiguity rather than picking a winner, and a
conformance suite. Converters carry dependencies — a video decoder, a mail
parser — and none of them belong in a package whose job is a catalog, so
they ship as their own distributions and this package never imports one.

**What a preparer promises**, and what :mod:`preparer_conformance` checks:
its output is admitted by the type it claims to produce, it is already in
that type's canonical form, and the same input twice produces the same
bytes. Determinism is the load-bearing one — a corpus that changes when it
is re-prepared re-checksums, and re-checksumming a corpus that has already
been annotated orphans the annotations.
"""

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar, Iterable

from strata.common import plugins

from .prepared import PreparedIndex, PreparedSample, relative_key

#: Where a distribution advertises the preparers it provides.
ENTRY_POINT_GROUP = "strata.preparers"


class PreparerError(Exception):
    """A conversion that cannot be found, or cannot be run on what it was given."""


@dataclass(frozen=True)
class Prepared:
    """One sample a conversion wrote, and what it knew about it."""

    #: The file written, inside the output directory.
    path: Path
    #: Recorded on the sample at ingest. What only the conversion knows —
    #: a sender, a capture time, the frame's index in its video.
    metadata: dict = field(default_factory=dict)
    #: Samples sharing one never straddle the train/val split. A video's
    #: frames are the case this exists for.
    group_id: str | None = None
    #: The annotation this sample arrived with, where the corpus came
    #: labelled. A candidate for a reviewer to correct, never an answer —
    #: see :class:`~strata.catalog.prepared.PreparedSample`.
    value: object | None = None


class Preparer:
    """A conversion from some source format into one sample type's shape."""

    #: How a project names this conversion.
    name: ClassVar[str] = ""

    #: The registered sample type this produces. The output has to satisfy
    #: that type — its extensions and its canonical form — which is what
    #: makes "prepare, then ingest" a contract rather than a convention.
    produces: ClassVar[str] = ""

    #: Extensions this consumes, lowercase and without the dot.
    sources: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        bad = [e for e in cls.sources if e != e.lower().lstrip(".")]
        if bad:
            raise PreparerError(
                f"{cls.__name__} lists source extension(s) {sorted(bad)} with a "
                f"dot or capitals. They are matched against a lowercased "
                f"suffix, so these would never match anything."
            )

    def allows(self, path: Path) -> bool:
        """Whether this conversion admits a source file, by extension."""
        suffix = Path(path).suffix.lower().lstrip(".")
        return bool(suffix) and suffix in self.sources

    def report(self) -> dict[str, int]:
        """What this conversion did not carry across, for a caller to print.

        A corpus arriving quietly smaller than the source it came from is
        the failure ingest already goes out of its way to avoid, and a
        conversion is the other place it can happen — a message with no
        body, one too long to be useful, a span that does not slice to its
        own text. Counted rather than logged, so one line at the end says
        what was left behind.
        """
        return {}

    def prepare(self, source: Path, out_dir: Path) -> Iterable[Prepared]:
        """Turn one source file into the samples it holds.

        One in, many out: a mailbox is a corpus, a video is a corpus. Write
        into ``out_dir`` and return what was written. Re-running over a
        source already converted must produce the same bytes at the same
        paths, or the corpus re-checksums and the annotations against it are
        orphaned.
        """
        raise NotImplementedError


# ----------------------------------------------------------------------
# Running one
# ----------------------------------------------------------------------


def run(
    preparer: Preparer,
    sources: Iterable[Path],
    out_dir: Path,
    on_source=None,
) -> PreparedIndex:
    """Convert ``sources`` into ``out_dir``, and record what was written.

    The index is merged with whatever is already there rather than replacing
    it, so converting a source directory that has grown adds to the corpus
    instead of forgetting the rest of it.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    written: dict[str, PreparedSample] = {}
    for source in sources:
        source = Path(source)
        if not preparer.allows(source):
            raise PreparerError(
                f"{type(preparer).__name__} does not admit {source.name} "
                f"(it reads: {', '.join('.' + e for e in sorted(preparer.sources))})"
            )
        for prepared in preparer.prepare(source, out_dir):
            written[relative_key(prepared.path, out_dir)] = PreparedSample(
                metadata=prepared.metadata,
                group_id=prepared.group_id,
                value=prepared.value,
            )
        if on_source is not None:
            on_source(source)

    index = PreparedIndex(produced_by=type(preparer).name, samples=written)
    existing = PreparedIndex.load(out_dir)
    if existing is not None:
        index = existing.merge(index)
    index.save(out_dir)
    return index


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------


def entries():
    """What is installed, as entry points. The seam a test stubs to pretend otherwise."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def available() -> dict[str, str]:
    """Registered preparer names, and what each resolves to."""
    return plugins.available(entries())


def resolve(name: str) -> type[Preparer]:
    """The class a preparer name refers to.

    A name registered twice is refused rather than resolved by whichever was
    loaded first: two conversions of the same corpus produce different bytes,
    and the one that runs would be decided by install order.
    """
    entry = plugins.find(entries(), name, what="preparer", error=PreparerError)
    return plugins.load(entry, Preparer, error=PreparerError)


def for_source(produces: str, path: Path) -> type[Preparer]:
    """The preparer that turns this file into ``produces``.

    Resolved from the pair rather than the extension alone: ``.json`` is a
    mailbox to one converter and something else entirely to another, and the
    sample type being aimed at is what separates them. Ambiguity is refused
    with both names, because picking one silently would decide what a corpus
    is made of.
    """
    candidates = []
    for name in sorted(available()):
        try:
            cls = resolve(name)
        except PreparerError:
            # A plugin whose import fails is not this call's problem to
            # report — resolve() by name says so much more usefully.
            continue
        if cls.produces == produces and cls().allows(path):
            candidates.append(cls)

    if not candidates:
        known = ", ".join(sorted(available())) or "none"
        raise PreparerError(
            f"Nothing installed here prepares {Path(path).suffix or 'that'} files "
            f"into '{produces}' samples. Installed preparers: {known}."
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(c.name for c in candidates))
        raise PreparerError(
            f"{len(candidates)} preparers turn {Path(path).suffix} files into "
            f"'{produces}' samples ({names}). Name one in [data] preparer."
        )
    return candidates[0]


__all__ = [
    "ENTRY_POINT_GROUP",
    "Prepared",
    "Preparer",
    "PreparerError",
    "available",
    "for_source",
    "resolve",
    "run",
]
