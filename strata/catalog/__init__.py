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

from .blobs import BlobBackend, LocalBackend, Location, checksum_of
from .catalog import Catalog, CatalogError, SampleRow
from .manifest import FILES_DIR, MANIFEST_NAME, Manifest, ManifestSample
from .split import SplitError, assign

__all__ = [
    "FILES_DIR",
    "MANIFEST_NAME",
    "BlobBackend",
    "Catalog",
    "CatalogError",
    "LocalBackend",
    "Location",
    "Manifest",
    "ManifestSample",
    "SampleRow",
    "SplitError",
    "assign",
    "checksum_of",
]
