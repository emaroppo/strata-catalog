"""The sample types this catalog provides.

Deliberately few. These are the kinds of data the schema already assumes
exist — an image, a document, a video frame — and anything more specific is
a plugin's business. They are registered through the same entry point group
as any plugin, so ``available()`` gives one answer rather than two.
"""

import unicodedata
from pathlib import Path
from typing import ClassVar

from .sample_types import SampleType, SampleTypeError


class Image(SampleType):
    """A picture, standing on its own."""

    media: ClassVar[str] = "image"
    extensions: ClassVar[frozenset[str]] = frozenset(
        {"jpg", "jpeg", "png", "webp", "bmp", "tif", "tiff", "gif"}
    )


class Frames(Image):
    """Video frames, one directory per video, each recording which video under ``video``.

    A project freezing its versions with ``group_by = "video"`` keeps a
    video's frames on one side of the split; one that does not treats each
    frame as its own sample.
    """

    segment: ClassVar[str] = "frames"

    #: The metadata key a frame's video is recorded under.
    VIDEO: ClassVar[str] = "video"

    def metadata_for(self, path: Path, root: Path) -> dict:
        # What extracted the frames knows which video they came from, and
        # says so; the directory is how a corpus nobody prepared says the
        # same thing.
        known = super().metadata_for(path, root)
        if known.get(self.VIDEO) is not None:
            return known
        relative = Path(path).resolve().relative_to(Path(root).resolve())
        # The directory, so a video is a group however deep it sits
        return {**known, self.VIDEO: relative.parent.as_posix()}


class Text(SampleType):
    """A document, read as characters rather than pixels.

    The one media where canonical form is load-bearing: a span is a pair of
    character offsets. See ``docs/adr/0010``.
    """

    media: ClassVar[str] = "text"
    extensions: ClassVar[frozenset[str]] = frozenset({"txt", "md"})

    def canonicalise(self, data: bytes) -> bytes:
        """UTF-8, no BOM, LF endings, NFC. Encoding is refused, not guessed.

        Converting an encoding is a preparer's job. See ``docs/adr/0010``.
        """
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise SampleTypeError(
                f"Not UTF-8 ({e.reason} at byte {e.start}). Text is stored as "
                f"UTF-8 so that a character offset means one thing; converting "
                f"an encoding is a preparer's job, since only it knows what "
                f"the corpus is."
            ) from None
        # Windows and classic-Mac endings alike. A reviewer's browser
        # normalises these on its own, so a document ingested with them
        # gives a model different offsets from the ones a human produced.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return unicodedata.normalize("NFC", text).encode("utf-8")
