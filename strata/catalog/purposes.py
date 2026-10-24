"""What a sample is for, beside what it is made of: the rules the catalog checks.

A type that is :class:`GeneratedMedia` arrives only through a generate run,
and a generate run writes only such types; any other way in is refused. A
type that is an :class:`SFTExample` carries its input in its metadata, and
a freeze over it compares that input across a sample's origins. Stored
samples name their type as media and subtype, so these read the class
back from what is installed. See ``docs/adr/0053``.
"""

from typing import TypeGuard

from strata.contracts import GeneratedMedia, SampleType, SampleTypeError, SFTExample
from strata.contracts import sample_types as registry
from strata.contracts.sample_types import PLAIN

from .rows import CatalogError


def installed(media: str, subtype: str) -> type[SampleType] | None:
    """The installed class stored as ``media/subtype``, or None if none is."""
    for name in registry.available():
        try:
            cls = registry.resolve(name)
        except SampleTypeError:
            # Refused where it is named; here it is simply not this one
            continue
        if cls.media == media and cls.subtype() == subtype:
            return cls
    return None


def nearest(media: str, subtype: str) -> type[SampleType] | None:
    """The installed class for ``media/subtype``, or for the nearest type above it."""
    segments = [] if subtype == PLAIN else subtype.split("/")
    while segments:
        cls = installed(media, "/".join(segments))
        if cls is not None:
            return cls
        segments.pop()
    return installed(media, PLAIN)


def is_generated(cls: type) -> bool:
    return issubclass(cls, GeneratedMedia)


def is_example(cls: type) -> TypeGuard[type[SFTExample]]:
    return issubclass(cls, SFTExample)


def refuse_generated(cls: type | None, name: str, how: str) -> None:
    """Refuse a generated type offered by anything but a generate run."""
    if cls is not None and is_generated(cls):
        raise CatalogError(
            f"{name} is generated media, which arrives only through a generate run, "
            f"with a record of what made it. {how} cannot write it. Nothing was written."
        )


def refuse_stored_generated(media: str, subtype: str, how: str) -> None:
    """:func:`refuse_generated` for a type named as it is stored."""
    refuse_generated(nearest(media, subtype), f"{media}/{subtype}", how)


def example_type(media: str, subtype: str) -> type[SFTExample] | None:
    """The fine-tuning type a sample stored as ``media/subtype`` is, or None.

    A type not installed here, below one that is a fine-tuning type, is
    refused: its own ``input_of`` may compare inputs otherwise, and a
    freeze cannot guess.
    """
    cls = installed(media, subtype)
    if cls is not None:
        return cls if is_example(cls) else None
    above = nearest(media, subtype)
    if above is not None and is_example(above):
        raise CatalogError(
            f"{media}/{subtype} is not installed here, and is a fine-tuning example "
            f"through {above.__name__}. Which inputs it compares, and how, is its own, "
            f"so install the package that provides it."
        )
    return None


__all__ = [
    "example_type",
    "installed",
    "is_example",
    "is_generated",
    "nearest",
    "refuse_generated",
    "refuse_stored_generated",
]
