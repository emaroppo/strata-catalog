"""The labelled data catalog: what samples exist, and what is known about them.

The durable asset, and it imports no tool. See ``docs/adr/0015``.

**May import:** ``contracts``, the standard library, and its own optional
storage drivers.

**May not import:** ``strata.labeller``, ``modelling``, Label Studio, or any ML
framework.

Two read paths, neither of which streams:

- ``materialise(query)`` — a self-contained directory of files plus a
  manifest carrying annotations, split and grouping. Dense and bulk: this is
  what training consumes, and the directory *is* the dataset version.
- ``url_for(sample)`` — sparse and random, for a review queue.

Storage and index are separate axes. Blobs go to a local directory or to
tars in object storage; the index is SQLite or Postgres against one schema.
See ``docs/adr/0021``.

"""

from .canonical import CanonicalError, canonical_form
from .catalog import Catalog
from .intake import Admission, IntakeError, admit
from .rows import EVERYTHING, AnnotateReport, CatalogError, CatalogMissing, DatasetRef, SampleRow
from .storage.blobs import BlobBackend, LocalBackend, Location, blob_path, checksum_of
from .storage.repack import RepackError, RepackReport, repack_blobs
from .storage.signing import SignedUrls, SigningError, suffix_of
from .sync.copy import CopyError, CopyReport, copy_index
from .sync.merge import MergeError, MergeReport, merge_annotations
from .versions.given import GivenSplit
from .versions.materialised import Materialised, ensure_materialised
from .versions.split import HOLDOUT, TRAIN, VAL, Achieved, SplitError, assign

#: Modules another package may import by path, a promise made knowingly
#: (``docs/adr/0015``). Anything not exported here and not listed is the
#: package's own, and the dependency graph test refuses it.
PUBLIC_MODULES = frozenset(
    {
        "canonical",
        "config",
        "intake",
        "stages",
        "versions.features",
    }
)

__all__ = [
    "EVERYTHING",
    "HOLDOUT",
    "TRAIN",
    "VAL",
    "Achieved",
    "Admission",
    "AnnotateReport",
    "BlobBackend",
    "CanonicalError",
    "Catalog",
    "CatalogError",
    "CatalogMissing",
    "CopyError",
    "CopyReport",
    "DatasetRef",
    "GivenSplit",
    "IntakeError",
    "LocalBackend",
    "Location",
    "Materialised",
    "MergeError",
    "MergeReport",
    "RepackError",
    "RepackReport",
    "SampleRow",
    "SignedUrls",
    "SigningError",
    "SplitError",
    "admit",
    "assign",
    "blob_path",
    "canonical_form",
    "checksum_of",
    "copy_index",
    "ensure_materialised",
    "merge_annotations",
    "repack_blobs",
    "suffix_of",
]
