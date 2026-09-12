"""A dataset version on disk: reused when it is the right one, rebuilt when not.

Two machines materialise dataset versions into a directory of their own —
the laptop for a local round, the modelling host for a remote one — named
by dataset and version, so a round retried after running out of memory
reuses the version rather than fetching it again. The rule for when a
directory already there may be reused lived in both places, and drifted:
the local copy learned that a change of features means a rebuild, the
host's did not, and remote rounds trained without features.

So it lives here, once. A directory is reused only when its manifest reads,
was built from this catalog, and was built with the same feature
declarations. Anything else is rebuilt: the directory is only a copy of what
the catalog holds, so rebuilding costs a fetch, while reusing the wrong one
costs a round trained on other data and a number reported for it.
"""

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from strata.labels import MANIFEST_NAME, Manifest, ManifestFormatError

from .features import FeatureSpec


class Materialised(NamedTuple):
    """A dataset version, as a directory a model can train from."""

    manifest: Manifest
    directory: Path
    #: Samples fetched to build it. Zero when a directory already there was
    #: reused, which is what lets a caller tell a cache hit from a transfer.
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

    ``catalog`` is anything with ``id``, ``datasets.named`` and
    ``materialise`` — a :class:`~strata.catalog.Catalog`, in practice.

    Asked before fetching, not after: materialising into staging only to
    find the version was already there meant pulling a whole dataset out of
    object storage to delete it.
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
        # An interrupted fetch. Its contents are unknowable — some files
        # written, no manifest — and keeping them would let a partial
        # dataset masquerade as a whole one.
        shutil.rmtree(staging)

    fetched = 0

    def tick(done: int, total: int) -> None:
        nonlocal fetched
        fetched = done
        if on_progress is not None:
            on_progress(done, total)

    catalog.materialise(dataset_id, staging, on_progress=tick, cache=cache, features=specs)
    # The manifest is written last, so a directory that has one is complete;
    # the rename is what makes it visible under its version at all.
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
        # Written by a release whose layout this one does not read, or before
        # manifests said which layout they were. Guessing at its fields could
        # cost a round trained on the wrong split.
        return None
    if manifest.catalog_id != catalog_id:
        # Another catalog's version with the same name and number — which is
        # every version, the first round after switching to a rebuilt
        # catalog, since its numbering starts again.
        return None
    if [dict(f) for f in manifest.features] != features:
        # Features are not part of a version's identity: they change what the
        # model is *told*, not which samples were chosen or what was said
        # about them. So a project that adds one keeps its version and needs
        # the directory rebuilt, or the round trains on a manifest written
        # before the feature existed.
        return None
    return manifest


__all__ = ["Materialised", "ensure_materialised"]
