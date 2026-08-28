"""What a sample is, and what that implies at ingest.

A photograph, a video frame, a satellite scene, a document. This belongs to
the catalog: it is a fact about the data, not about whatever tool collected
it, and it decides four things nothing else can.

- **Which files are allowed.** An allow list, checked and reported — not a
  filter applied quietly. Ingest walks what it is pointed at, because a
  sensibly arranged folder is the user's job; what it must never do is take
  in less than it was given without saying so.
- **What is recorded about each one.** A satellite scene has bounds and a
  capture time; a frame has an index. The catalog holds arbitrary metadata
  per sample and has never had a way to fill it.
- **How samples group.** Frames of one video must not straddle a train/val
  split. Grouping is per sample in the schema already; this is the rule that
  assigns it.
- **What canonical form the bytes are in.** A line ending or a byte order
  mark is not a difference between two documents, but it is a difference
  between two checksums — and for anything annotated by character offset it
  is a difference between two sets of offsets. Semantically null, idempotent
  and safe at ingest; see :meth:`SampleType.canonicalise` for the line
  between this and normalisation, which is not safe there at all.

Types are plugins, registered through the ``strata.sample_types`` entry
point group, and they inherit. ``Satellite(Image)`` overrides how metadata
is read and keeps everything else.

**Inheritance is safe here, and is not for label sets.** A label set's
classes map to a checkpoint's output neurons by position, so a parent
gaining one silently reindexes its children — which is why label sets are
copied rather than inherited. A type is code. Nothing acts at a distance.

**Substitution has to hold in two places.** Where an image is expected a
satellite scene should do: in code that is ``issubclass``, and in queries it
is a subtype *path* matched by prefix, the same rule collections use.
``satellite`` matches ``satellite/multispectral`` and never
``satellite_old``. The stored pair is a denormalisation of the class chain,
so both are checked when a type is defined.
"""

from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar

from .prepared import index_for

#: Where a distribution advertises the sample types it provides.
ENTRY_POINT_GROUP = "strata.sample_types"

#: What the unspecialised case of a media is stored as. Every media has one,
#: and it is what a sample was before anyone thought about subtypes.
PLAIN = "plain"


class SampleTypeError(Exception):
    """A type that cannot be defined, found, or used for the data at hand."""


class SampleType:
    """A kind of sample, and what ingest should do with it.

    Subclass to specialise. A subclass declares a ``segment`` naming what it
    adds, and inherits everything it does not override.
    """

    #: Which media this is. May not change in a subclass: a query for images
    #: has to keep returning satellite scenes, and nothing would report it
    #: if one stopped.
    media: ClassVar[str] = ""

    #: What this adds below its parent. Empty for the unspecialised case of
    #: a media, which stores as ``plain``.
    segment: ClassVar[str] = ""

    #: Extensions this type admits, lowercase and without the dot. Checked
    #: rather than used to discover, so a file outside the list is reported
    #: rather than skipped in silence.
    extensions: ClassVar[frozenset[str]] = frozenset()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        parent = cls.__mro__[1]
        inherited = getattr(parent, "media", "")
        if inherited and cls.media != inherited:
            raise SampleTypeError(
                f"{cls.__name__} declares media {cls.media!r} but inherits from "
                f"{parent.__name__}, which is {inherited!r}. A subtype cannot "
                f"change its media: a query for {inherited!r} would silently "
                f"stop returning it."
            )
        if "/" in cls.segment:
            raise SampleTypeError(
                f"{cls.__name__} segment {cls.segment!r} contains a '/'. Depth "
                f"comes from inheriting, so the path and the class chain "
                f"cannot disagree."
            )

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @classmethod
    def subtype(cls) -> str:
        """The stored path: every segment from the media's base down to here.

        Built from the class chain rather than declared, so the string a
        query matches on and the hierarchy code substitutes through cannot
        drift apart.
        """
        segments = [
            klass.segment
            for klass in reversed(cls.__mro__)
            if isinstance(klass, type)
            and issubclass(klass, SampleType)
            and klass.__dict__.get("segment")
        ]
        return "/".join(segments) or PLAIN

    # ------------------------------------------------------------------
    # What ingest asks
    # ------------------------------------------------------------------

    def allows(self, path: Path) -> bool:
        """Whether this type admits a file, by extension."""
        suffix = Path(path).suffix.lower().lstrip(".")
        return bool(suffix) and suffix in self.extensions

    def canonicalise(self, data: bytes) -> bytes:
        """These bytes in the one form the catalog stores them in.

        Semantically null and idempotent: a line ending, a byte order mark,
        a unicode composition. What it buys is an honest checksum — two
        files that read identically should not ingest as two samples — and,
        for anything annotated by character offset, one answer to what the
        characters are.

        **The test that separates this from normalisation:** would two
        independent implementations produce identical bytes? If yes it is
        canonicalisation and belongs here. If it encodes a preference —
        column names, key order, whitespace someone prefers — it does not.
        A checksum would then depend on our own version, and ``merge``
        matches samples on checksum precisely so that two hosts on
        different releases still agree about what a sample is.

        The default returns the bytes untouched, and ingest reads no file at
        all for a type that leaves it that way.
        """
        return data

    @classmethod
    def canonicalises(cls) -> bool:
        """Whether this type has a canonical form worth checking for.

        Asked rather than assumed so ingest can keep its cheap path: a
        corpus of images is hardlinked without ever being read, and only a
        type that overrides :meth:`canonicalise` pays for the read.
        """
        return cls.canonicalise is not SampleType.canonicalise

    def metadata_for(self, path: Path, root: Path) -> dict:
        """What to record about this sample beyond its bytes.

        Where it came from is added by ingest itself; this is for what only
        the type knows — a capture time, a coordinate system, a frame index.

        The default hands back whatever a conversion recorded for this file.
        A type that overrides this and still wants that should call
        ``super()`` — prepared metadata is a fact about the corpus, not
        about the type reading it.
        """
        prepared = index_for(root)
        entry = prepared.entry_for(path, root) if prepared is not None else None
        return dict(entry.metadata) if entry is not None else {}

    def group_id_for(self, path: Path, root: Path) -> str | None:
        """Which group this sample belongs to, or None for its own.

        Samples sharing a group are never split across train and validation,
        because near-duplicates on both sides make a validation score
        meaningless.

        The default is what a conversion declared. That is the better place
        for it: a converter turning one video into frames knows they are one
        video, where a type can only infer it from a directory layout both
        sides have to agree about.
        """
        prepared = index_for(root)
        entry = prepared.entry_for(path, root) if prepared is not None else None
        return entry.group_id if entry is not None else None


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------


def _entries():
    return list(entry_points(group=ENTRY_POINT_GROUP))


def _builtin_names() -> set[str]:
    """Names this package itself provides, which a plugin may not take."""
    return {
        entry.name
        for entry in _entries()
        if getattr(getattr(entry, "dist", None), "name", None) == "strata-catalog"
    }


def available() -> dict[str, str]:
    """Registered type names, and what each resolves to.

    Read from what is installed rather than a list someone maintains, which
    is the only honest answer to what this catalog can ingest.
    """
    return {entry.name: entry.value for entry in _entries()}


def resolve(name: str) -> type[SampleType]:
    """The class a type name refers to.

    Built-in names are reserved. A plugin registering ``image`` would change
    how everything is ingested and nothing would report it, so the clash is
    refused rather than resolved by whichever was loaded first.
    """
    matches = [entry for entry in _entries() if entry.name == name]
    if not matches:
        known = ", ".join(sorted(available())) or "none"
        raise SampleTypeError(
            f"No sample type named {name!r}. Installed here: {known}."
        )
    if len(matches) > 1:
        builtin = _builtin_names()
        owners = ", ".join(
            getattr(getattr(entry, "dist", None), "name", "?") for entry in matches
        )
        detail = (
            f"{name!r} is provided by this catalog and cannot be replaced"
            if name in builtin
            else f"{name!r} is registered more than once"
        )
        raise SampleTypeError(f"{detail} (from: {owners}).")

    loaded = matches[0].load()
    if not (isinstance(loaded, type) and issubclass(loaded, SampleType)):
        raise SampleTypeError(
            f"{name!r} resolves to {loaded!r}, which is not a SampleType."
        )
    return loaded
