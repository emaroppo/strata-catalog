"""Looking after a catalog: what is in it, whether it is reachable, what is installed.

The operations behind ``strata-catalog``, as functions returning records.
Copying, merging and repacking live in ``sync`` and ``storage``. See
``docs/adr/0030``.
"""

from .installed import CatalogEntry, PreparerEntry, TypeEntry, catalogs, preparers, types
from .probe import BlobProbe, IndexProbe, Probe, probe
from .remove import RemoveReport, remove
from .stats import LabelSetStats, Stats, stats
from .where import redacted, where_blobs, where_index

__all__ = [
    "BlobProbe",
    "CatalogEntry",
    "IndexProbe",
    "LabelSetStats",
    "PreparerEntry",
    "Probe",
    "RemoveReport",
    "Stats",
    "TypeEntry",
    "catalogs",
    "preparers",
    "probe",
    "redacted",
    "remove",
    "stats",
    "types",
    "where_blobs",
    "where_index",
]
