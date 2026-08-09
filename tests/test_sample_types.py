"""What a sample is, and the two invariants that keep it honest.

Both failures this guards against are silent: a subtype that changes its
media stops being returned by a query for that media, and a stored path that
does not match the class chain makes "is this an image" answerable two ways.
"""

from pathlib import Path

import pytest

from strata.catalog.builtin_types import Frames, Image, Text
from strata.catalog.sample_types import (
    PLAIN,
    SampleType,
    SampleTypeError,
    available,
    resolve,
)

# ----------------------------------------------------------------------
# The path is the class chain
# ----------------------------------------------------------------------


def test_an_unspecialised_type_is_plain():
    assert Image.subtype() == PLAIN
    assert Text.subtype() == PLAIN


def test_a_subtype_names_what_it_adds():
    assert Frames.subtype() == "frames"


def test_depth_comes_from_inheriting():
    class Satellite(Image):
        segment = "satellite"

    class Multispectral(Satellite):
        segment = "multispectral"

    # Selecting 'satellite' has to match this without knowing it exists,
    # which is the same prefix rule collections use
    assert Satellite.subtype() == "satellite"
    assert Multispectral.subtype() == "satellite/multispectral"
    assert Multispectral.subtype().startswith(Satellite.subtype() + "/")


def test_substitution_holds_in_code():
    class Satellite(Image):
        segment = "satellite"

    # Where an image is expected, a satellite scene does
    assert issubclass(Satellite, Image)
    assert Satellite.media == Image.media


# ----------------------------------------------------------------------
# What is refused, and why
# ----------------------------------------------------------------------


def test_a_subtype_cannot_change_its_media():
    with pytest.raises(SampleTypeError, match="cannot"):

        class Raster(Image):
            media = "raster"
            segment = "raster"


def test_a_segment_cannot_carry_its_own_path():
    # Depth comes from inheriting; a slash here would let the stored path
    # and the class chain disagree
    with pytest.raises(SampleTypeError, match="'/'"):

        class Nested(Image):
            segment = "satellite/multispectral"


def test_a_type_with_no_parent_media_is_free_to_declare_one():
    class Audio(SampleType):
        media = "audio"
        extensions = frozenset({"wav"})

    assert Audio.subtype() == PLAIN


# ----------------------------------------------------------------------
# Extensions are checked, not used to discover
# ----------------------------------------------------------------------


def test_an_allowed_extension_is_admitted():
    assert Image().allows(Path("a/b/photo.JPG"))
    assert Text().allows(Path("notes.md"))


def test_anything_else_is_not():
    # Reported by the caller rather than skipped in silence: a corpus
    # quietly smaller than the directory it came from is the failure this
    # list exists to make visible
    assert not Image().allows(Path("notes.md"))
    assert not Image().allows(Path("no-extension"))


def test_a_subtype_inherits_what_it_admits():
    assert Frames().allows(Path("vid1/frame001.jpg"))


# ----------------------------------------------------------------------
# Grouping
# ----------------------------------------------------------------------


def test_a_plain_sample_is_its_own_group():
    assert Image().group_id_for(Path("/data/a.jpg"), Path("/data")) is None


def test_frames_group_by_the_directory_they_sit_in(tmp_path):
    (tmp_path / "vid1").mkdir()
    first = tmp_path / "vid1" / "f001.jpg"
    second = tmp_path / "vid1" / "f002.jpg"
    other = tmp_path / "vid2"
    other.mkdir()
    third = other / "f001.jpg"
    for path in (first, second, third):
        path.write_bytes(b"x")

    frames = Frames()
    # Consecutive frames are near-duplicates; a split that separates them
    # scores a model on what it memorised
    assert frames.group_id_for(first, tmp_path) == frames.group_id_for(second, tmp_path)
    assert frames.group_id_for(third, tmp_path) != frames.group_id_for(first, tmp_path)


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------


def test_the_built_ins_are_registered_like_any_plugin():
    assert {"image", "frames", "text"} <= set(available())


def test_resolving_gives_the_class():
    assert resolve("frames") is Frames


def test_an_unknown_type_says_what_is_installed():
    with pytest.raises(SampleTypeError, match="image"):
        resolve("satellite")
