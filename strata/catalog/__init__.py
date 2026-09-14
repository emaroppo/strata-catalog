"""The labelled data catalog: what samples exist, and what is known about them.

The durable asset. Everything else in this workspace is a producer or a
consumer of what lives here, and the catalog must not depend on either —
annotations outlive the tool that collected them.

**May import:** ``labels``, the standard library, and its own optional
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
The local pair needs no infrastructure, which is what keeps the repository
runnable by someone who just cloned it.

Phase 2 built the local half: SQLite index, a directory of blobs, ingest and
``materialise``. The S3 backend and Postgres are later phases behind the same
interface.
"""

from .catalog import Catalog
from .rows import EVERYTHING, AnnotateReport, CatalogError, CatalogMissing, DatasetRef, SampleRow
from .storage.blobs import BlobBackend, LocalBackend, Location, blob_path, checksum_of
from .storage.repack import RepackError, RepackReport, repack_blobs
from .storage.signing import SignedUrls, SigningError, suffix_of
from .sync.copy import CopyError, CopyReport, copy_index
from .sync.merge import MergeError, MergeReport, merge_annotations
from .types.prepared import PREPARED_NAME, PreparedIndex, PreparedSample
from .types.preparers import Prepared, Preparer, PreparerError
from .versions.given import GivenSplit
from .versions.materialised import Materialised, ensure_materialised
from .versions.split import HOLDOUT, TRAIN, VAL, Achieved, SplitError, assign

#: Modules another package may import by path, a promise made knowingly
#: (``docs/adr/0015``). Anything not exported here and not listed is the
#: package's own, and the dependency graph test refuses it.
PUBLIC_MODULES = frozenset(
    {
        "config",
        "stages",
        "types.prepared",
        "types.preparers",
        "types.sample_types",
        "versions.features",
    }
)

__all__ = [
    "EVERYTHING",
    "HOLDOUT",
    "PREPARED_NAME",
    "TRAIN",
    "VAL",
    "Achieved",
    "AnnotateReport",
    "BlobBackend",
    "Catalog",
    "CatalogError",
    "CatalogMissing",
    "CopyError",
    "CopyReport",
    "DatasetRef",
    "GivenSplit",
    "LocalBackend",
    "Location",
    "Materialised",
    "MergeError",
    "MergeReport",
    "Prepared",
    "PreparedIndex",
    "PreparedSample",
    "Preparer",
    "PreparerError",
    "RepackError",
    "RepackReport",
    "SampleRow",
    "SignedUrls",
    "SigningError",
    "SplitError",
    "assign",
    "blob_path",
    "checksum_of",
    "copy_index",
    "ensure_materialised",
    "merge_annotations",
    "repack_blobs",
    "suffix_of",
]
