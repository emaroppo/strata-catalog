"""The labelled data catalog: what samples exist, and what is known about them.

Imports ``contracts`` and the standard library, never a tool. Blobs and index
are separate axes behind one schema. See ``docs/adr/0015``, ``docs/adr/0021``.
"""

from .admin.where import redacted
from .canonical import CanonicalError, canonical_form
from .catalog import Catalog
from .intake import Admission, IntakeError, admit
from .rows import (
    EVERYTHING,
    AmbiguousSample,
    AnnotateReport,
    CatalogError,
    CatalogMissing,
    DatasetRef,
    SampleRow,
    split_type,
)
from .selection import SelectionSpec
from .storage.blobs import BlobBackend, LocalBackend, Location, blob_path, checksum_of
from .storage.repack import RepackError, RepackReport, repack_blobs
from .storage.signing import SignedUrls, SigningError, suffix_of
from .sync.copy import CopyError, CopyReport, copy_index
from .sync.merge import MergeError, MergeReport, merge_annotations
from .verify import DatasetVerifyReport, verify_dataset
from .versions.given import GivenSplit
from .versions.materialised import Materialised, ensure_materialised
from .versions.split import HOLDOUT, TRAIN, VAL, Achieved, SplitError, assign

#: Modules another package may import by path; the dependency test refuses the rest. docs/adr/0015
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
    "AmbiguousSample",
    "AnnotateReport",
    "BlobBackend",
    "CanonicalError",
    "Catalog",
    "CatalogError",
    "CatalogMissing",
    "CopyError",
    "CopyReport",
    "DatasetRef",
    "DatasetVerifyReport",
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
    "SelectionSpec",
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
    "redacted",
    "repack_blobs",
    "split_type",
    "suffix_of",
    "verify_dataset",
]
