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
    """Video frames, one directory per video.

    The grouping is the whole point. Consecutive frames are near-duplicates,
    so a split that puts some of a video in train and the rest in validation
    scores a model on what it has already memorised — which reads as a very
    good model.
    """

    segment: ClassVar[str] = "frames"

    def group_id_for(self, path: Path, root: Path) -> str:
        # What extracted the frames knows which video they came from, and
        # says so; the directory is how a corpus nobody prepared says the
        # same thing.
        declared = super().group_id_for(path, root)
        if declared is not None:
            return declared
        relative = Path(path).resolve().relative_to(Path(root).resolve())
        # The directory, so a video is a group however deep it sits
        return relative.parent.as_posix()


class Text(SampleType):
    """A document, read as characters rather than pixels.

    The one media where canonical form is load-bearing rather than tidy. A
    span annotation is a pair of character offsets, so the reviewer's
    browser and the model's tokenizer have to be reading the same
    characters; a document that is UTF-8 with LF endings everywhere is how
    that is guaranteed rather than hoped for.
    """

    media: ClassVar[str] = "text"
    extensions: ClassVar[frozenset[str]] = frozenset({"txt", "md"})

    def canonicalise(self, data: bytes) -> bytes:
        """UTF-8, no BOM, LF endings, NFC.

        Four rules, each of which two independent implementations would
        agree on, and none of which changes what the document says.

        Encoding is refused rather than guessed. A mojibake document is
        worse than a rejected one: it ingests, it displays as something
        plausible, and every offset annotated against it is against
        characters that were never there. Converting an encoding is a
        conversion, and conversions belong to a preparer, which knows what
        the corpus actually is.
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
