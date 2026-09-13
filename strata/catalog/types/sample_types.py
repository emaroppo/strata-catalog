"""What a sample is, and what that implies at ingest.

A type decides four things: which files are admitted, what is recorded
about each, how samples group, and what canonical form their bytes take.
Types are plugins under the ``strata.sample_types`` entry point group, and
they inherit: ``Satellite(Image)`` overrides one method and keeps the
rest. In queries a subtype is a path matched by prefix, the rule
collections use. See ``docs/adr/0010``.
"""

from importlib.metadata import entry_points
from pathlib import Path
from typing import ClassVar

from strata.common import plugins

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

        Semantically null and idempotent. The test: would two independent
        implementations produce identical bytes? If it encodes a
        preference it is normalisation and does not belong here. The
        default returns the bytes untouched, and ingest reads no file for
        a type that leaves it that way. See ``docs/adr/0010``.
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
        the type knows — a capture time, a coordinate system, a frame index,
        the video a frame belongs to. A grouping is a key in here like any
        other: nothing about it is special until a version is frozen with
        ``group_by`` naming the key.

        The default hands back whatever a conversion recorded for this file.
        A type that overrides this and still wants that should call
        ``super()`` — prepared metadata is a fact about the corpus, not
        about the type reading it.
        """
        prepared = index_for(root)
        entry = prepared.entry_for(path, root) if prepared is not None else None
        return dict(entry.metadata) if entry is not None else {}


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------


def entries():
    """What is installed, as entry points. The seam a test stubs to pretend otherwise."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def _builtin_names() -> set[str]:
    """Names this package itself provides, which a plugin may not take."""
    return {
        entry.name
        for entry in entries()
        if getattr(getattr(entry, "dist", None), "name", None) == "strata-catalog"
    }


def available() -> dict[str, str]:
    """Registered type names, and what each resolves to."""
    return plugins.available(entries())


def resolve(name: str) -> type[SampleType]:
    """The class a type name refers to. Built-in names are reserved."""
    entry = plugins.find(
        entries(), name, what="sample type", error=SampleTypeError, reserved=_builtin_names()
    )
    return plugins.load(entry, SampleType, error=SampleTypeError)
