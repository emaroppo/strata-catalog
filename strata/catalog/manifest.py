"""The manifest: a materialised dataset's description of itself.

Written beside the files, and complete enough that nothing needs a database
to train from it. That is what makes a dataset version the portable unit —
the property ``project.py`` used to claim for a whole project directory, at
a granularity that survives the move to a catalog.

Sample ids are catalog-local, so ``checksum`` travels with them: files and
labels alone are enough to train anywhere, and the checksum is what lets a
different catalog match these samples to its own.
"""

from pydantic import BaseModel, Field

from strata.labels import AnySchema, AnyValue

MANIFEST_NAME = "manifest.json"
FILES_DIR = "files"


class ManifestSample(BaseModel):
    """One sample as a trainer sees it."""

    id: int
    checksum: str
    #: Relative to the manifest's own directory, so the whole thing moves.
    path: str
    #: Shared by samples that must not straddle the split; null means the
    #: sample is its own group.
    group_id: str | None = None
    val: bool = False
    #: Null when the sample was skipped. An empty value is different: a
    #: human looked and found nothing, which is an answer.
    #:
    #: Any annotation payload, not one task's — a dataset version is the
    #: artifact a model trains from, so pinning it to choices would mean no
    #: detector could ever be handed one.
    value: AnyValue | None = None


class Manifest(BaseModel):
    """A dataset version, in full."""

    dataset: str
    version: int
    #: Which catalog this was built from. A dataset name and a sample id
    #: both mean something only within one, so a directory that does not
    #: say makes a run's lineage unresolvable the moment a host serves two.
    #: Null for a version materialised before catalogs had identities.
    catalog_id: str | None = None
    label_set: str
    label_schema: AnySchema
    #: What was asked for, and what grouping actually allowed. They differ
    #: when a group is too large to hold out at the requested ratio.
    val_ratio: float = 0.2
    val_ratio_achieved: float = 0.0
    samples: list[ManifestSample] = Field(default_factory=list)

    @property
    def train(self) -> list[ManifestSample]:
        return [s for s in self.samples if not s.val]

    @property
    def val(self) -> list[ManifestSample]:
        return [s for s in self.samples if s.val]
