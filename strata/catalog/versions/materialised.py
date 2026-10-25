"""A dataset version on disk: reused only when its manifest proves it, rebuilt otherwise.

See ``docs/adr/0026``.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from strata.contracts import MANIFEST_NAME, Manifest, ManifestFormatError

from .features import FeatureSpec
from .inputs import INPUT


class Materialised(NamedTuple):
    """A dataset version, as a directory a model can train from."""

    manifest: Manifest
    directory: Path
    #: Samples fetched to build it; zero when the directory there was reused.
    fetched: int


def ensure_materialised(
    catalog,
    dataset_id: int,
    root: Path,
    *,
    features: Sequence[FeatureSpec] = (),
    on_progress=None,
    cache: Path | None = None,
) -> Materialised:
    """The directory for a dataset version under ``root``, built if need be.

    ``catalog`` is anything with ``id``, ``datasets.named`` and ``materialise``. See
    ``docs/adr/0026``.
    """
    ref = catalog.datasets.named(dataset_id)
    name = ref.name
    directory = Path(root) / name / f"v{ref.version:03d}"
    specs = list(features)

    existing = _reusable(directory, catalog.id, [spec.as_dict() for spec in specs])
    if existing is not None:
        return Materialised(existing, directory, 0)
    if directory.exists():
        shutil.rmtree(directory)

    staging = Path(root) / name / "pending"
    if staging.exists():
        # An interrupted fetch, discarded. docs/adr/0026
        shutil.rmtree(staging)

    fetched = 0

    def tick(done: int, total: int) -> None:
        nonlocal fetched
        fetched = done
        if on_progress is not None:
            on_progress(done, total)

    catalog.materialise(dataset_id, staging, on_progress=tick, cache=cache, features=specs)
    # Manifest last, then the rename: a visible directory with a manifest is whole. docs/adr/0026
    staging.rename(directory)
    manifest = Manifest.model_validate_json((directory / MANIFEST_NAME).read_text())
    return Materialised(manifest, directory, fetched)


def _reusable(directory: Path, catalog_id: str | None, features: list[dict]) -> Manifest | None:
    """The manifest already in ``directory``, if the version there may be reused."""
    try:
        manifest = Manifest.model_validate_json((directory / MANIFEST_NAME).read_text())
    except FileNotFoundError:
        return None
    except ManifestFormatError:
        # A layout this release does not read is rebuilt, never guessed at. docs/adr/0026
        return None
    if manifest.catalog_id != catalog_id:
        # Another catalog's version with the same name and number. docs/adr/0008
        return None
    declared = [dict(f) for f in manifest.features if f.get("source") != INPUT]
    if declared != features:
        # Features are no part of a version's identity. docs/adr/0026, docs/adr/0053
        return None
    return manifest


__all__ = ["Materialised", "ensure_materialised"]
