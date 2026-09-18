"""The one form a catalog stores a sample's bytes in.

Canonical form is the catalog's: it decides what a checksum addresses, so
two files that read identically are one sample. It is semantically null and
idempotent, and normalisation does not belong here. Each media has a
default, of which only text's is not the identity; a plugin type that needs
its own registers one under the ``strata.canonical_forms`` entry point
group by the type's name, and a type without one takes the nearest
ancestor's, then its media's.

A preparer that ships annotations against a file's characters writes the
file in this form already, and a catalog refuses a candidate annotation on
bytes this would change. See ``docs/adr/0010`` and ``docs/adr/0040``.
"""

import unicodedata
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import cast

from strata.contracts import SampleType
from strata.contracts import sample_types as registry

#: Where a distribution advertises a canonical form for the type it names.
ENTRY_POINT_GROUP = "strata.canonical_forms"

Canonicalise = Callable[[bytes], bytes]


class CanonicalError(Exception):
    """Bytes that cannot be put in the form their type is stored in."""


def text(data: bytes) -> bytes:
    """UTF-8, no BOM, LF endings, NFC. Encoding is refused, not guessed.

    Converting an encoding is a preparer's job, since only it knows what the
    corpus is. See ``docs/adr/0010``.
    """
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        decoded = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CanonicalError(
            f"Not UTF-8 ({e.reason} at byte {e.start}). Text is stored as "
            f"UTF-8 so that a character offset means one thing; converting "
            f"an encoding is a preparer's job, since only it knows what "
            f"the corpus is."
        ) from None
    # Windows and classic-Mac endings alike. docs/adr/0010
    decoded = decoded.replace("\r\n", "\n").replace("\r", "\n")
    return unicodedata.normalize("NFC", decoded).encode("utf-8")


#: Each media's default. A media absent here stores its bytes untouched, and
#: ingest reads no file for it: a corpus of images is hardlinked.
BY_MEDIA: dict[str, Canonicalise] = {"text": text}


def entries():
    """What is installed, as entry points. The seam a test stubs to pretend otherwise."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def canonical_form(sample_type: type[SampleType]) -> Canonicalise | None:
    """How a type's bytes are stored, or None where they are stored as they are.

    The form registered for the nearest class in the type's chain, else its
    media's default.
    """
    registered = {entry.name: entry for entry in entries()}
    if registered:
        for klass in sample_type.__mro__:
            if not (isinstance(klass, type) and issubclass(klass, SampleType)):
                continue
            for name in _names_of(klass, registered):
                form = registered[name].load()
                if not callable(form):
                    raise CanonicalError(
                        f"The canonical form registered for {name!r} is {form!r}, "
                        f"which is not a function of bytes."
                    )
                return cast(Canonicalise, form)
    return BY_MEDIA.get(sample_type.media)


def _names_of(klass: type[SampleType], registered: dict) -> list[str]:
    """The registered type names that resolve to ``klass`` and have a form."""
    names = []
    for name in sorted(set(registered) & set(registry.available())):
        try:
            if registry.resolve(name) is klass:
                names.append(name)
        except registry.SampleTypeError:
            # A type that does not resolve is refused where it is named;
            # here it simply has no form to offer.
            continue
    return names


__all__ = [
    "BY_MEDIA",
    "ENTRY_POINT_GROUP",
    "CanonicalError",
    "Canonicalise",
    "canonical_form",
    "entries",
    "text",
]
