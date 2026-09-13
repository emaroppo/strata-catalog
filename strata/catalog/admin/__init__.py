"""Looking after a catalog: what is in it, whether it is reachable, what is installed.

The operations behind ``strata-catalog``, as functions returning records,
so the command renders and the orchestrator or a test reads. Copying,
merging and repacking live in ``sync`` and ``storage`` and return their
own reports; this holds the ones that were only ever a command body.
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
