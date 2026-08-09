"""The sample types this catalog provides.

Deliberately few. These are the kinds of data the schema already assumes
exist — an image, a document, a video frame — and anything more specific is
a plugin's business. They are registered through the same entry point group
as any plugin, so ``available()`` gives one answer rather than two.
"""

from pathlib import Path
from typing import ClassVar

from .sample_types import SampleType


class Image(SampleType):
    """A picture, standing on its own."""

    media: ClassVar[str] = "image"
    extensions: ClassVar[frozenset[str]] = frozenset(
        {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "gif"}
    )


class Frames(Image):
    """Video frames, one directory per video.

    The grouping is the whole point. Consecutive frames are near-duplicates,
    so a split that puts some of a video in train and the rest in validation
    scores a model on what it has already memorised — which reads as a very
    good model.
    """

    segment: ClassVar[str] = "frames"

    def group_id_for(self, path: Path, root: Path) -> str:
        relative = Path(path).resolve().relative_to(Path(root).resolve())
        # The directory, so a video is a group however deep it sits
        return relative.parent.as_posix()


class Text(SampleType):
    """A document, read as characters rather than pixels."""

    media: ClassVar[str] = "text"
    extensions: ClassVar[frozenset[str]] = frozenset({"txt", "md"})
