"""The catalog's stages: what an experiment asks of it, as requests and records.

Three of them. ``dataset`` freezes a version, ``materialise`` puts it on
disk, and ``split`` decides — or, by default, reads — which side each
sample is on. Each takes a request and a context and returns a record that
names what it made by identity: a dataset id, a directory. See
``docs/adr/0030``.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from strata.common.canonical import short_hash
from strata.common.stages import Stage
from strata.labels import MANIFEST_NAME, Manifest

from .catalog import Catalog
from .rows import CatalogError
from .versions.features import FeatureSpec
from .versions.files import link_or_copy
from .versions.given import GivenSplit
from .versions.materialised import ensure_materialised
from .versions.split import HOLDOUT, TRAIN, VAL, assign

#: The kinds these stages link, for a chain to be checked before it runs.
DATASET_VERSION = "dataset_version"
DATASET_DIR = "dataset_dir"


#: Which side of a split a sample is on.
Side = Literal["train", "val", "holdout"]


class Strict(BaseModel):
    """Unknown keys are an error. See ``docs/adr/0030``."""

    model_config = ConfigDict(extra="forbid")


@dataclass
class Context:
    """The handles a catalog stage needs: the catalog, and where versions land."""

    catalog: Catalog
    #: Where materialised versions live on this host, ``<name>/v<version>``
    #: under it.
    datasets_dir: Path
    #: Blobs already on this host, consulted before the backend.
    cache: Path | None = None
    #: ``on_progress(done, total)`` as blobs land.
    on_progress: Callable[[int, int], None] | None = None


# ----------------------------------------------------------------------
# dataset
# ----------------------------------------------------------------------


class DatasetRequest(Strict):
    """Freeze what is labelled for a label set into a version."""

    name: str
    label_set: str
    collections: list[str]
    val_ratio: float = 0.2
    holdout_ratio: float = 0.0
    seed: int = 42
    #: A metadata key whose values stay on one side. None: no grouping.
    group_by: str | None = None
    #: False re-splits from nothing rather than keeping the previous
    #: version's sides; the version records it, and a warm start stops there.
    #: See docs/adr/0024.
    inherit: bool = True
    #: A split the corpus arrived with: the metadata key naming each
    #: sample's set, and which names are holdout and which val. The samples
    #: it names are fixed; the rest are drawn.
    given: GivenSplit | None = None


class DatasetRecord(Strict):
    """A frozen version, by identity, and what it says about itself."""

    dataset_id: int
    name: str
    version: int
    annotation_digest: str | None
    catalog_id: str
    label_set_id: int
    samples: int
    #: How many samples' sides the corpus gave rather than the draw.
    given: int = 0
    #: How many groups the given sides cut across. Reproduced rather than
    #: corrected, and counted. See docs/adr/0024.
    groups_cut: int = 0


def dataset(request: DatasetRequest, context: Context) -> DatasetRecord:
    catalog = context.catalog
    try:
        label_set_id, _ = catalog.label_sets.get(request.label_set)
    except CatalogError as exc:
        raise CatalogError(
            f"No label set named {request.label_set!r} in the catalog. "
            f"Run ingest first, or set [catalog] label_set."
        ) from exc
    labelled = catalog.samples.labelled(label_set_id, request.collections)
    if not labelled:
        raise CatalogError(
            f"Nothing is labelled for {request.label_set!r} in "
            f"{', '.join(request.collections)}, so there is nothing to train on."
        )
    dataset_id = catalog.create_dataset(
        request.name,
        label_set_id,
        collections=request.collections,
        val_ratio=request.val_ratio,
        holdout_ratio=request.holdout_ratio,
        seed=request.seed,
        group_by=request.group_by,
        inherit=request.inherit,
        given=request.given,
    )
    ref = catalog.datasets.named(dataset_id)
    given = 0
    if request.given is not None:
        given = len(request.given.sides_for({s.id: s.metadata or {} for s in labelled}))
    return DatasetRecord(
        dataset_id=dataset_id,
        name=ref.name,
        version=ref.version,
        annotation_digest=ref.annotation_digest,
        catalog_id=catalog.id,
        label_set_id=label_set_id,
        samples=len(labelled),
        given=given,
        groups_cut=catalog.datasets.groups_cut(dataset_id, label_set_id, request.group_by),
    )


# ----------------------------------------------------------------------
# materialise
# ----------------------------------------------------------------------


class MaterialiseRequest(Strict):
    """A version as a directory a model can train from."""

    dataset_id: int
    #: Feature declarations, ``{name, source, ref}``, as a project writes
    #: them. Declarations only: the values are read from the catalog here.
    features: list[dict] = Field(default_factory=list)


class MaterialiseRecord(Strict):
    directory: Path
    dataset: str
    version: int
    #: Samples fetched to build it. Zero when a directory already there was
    #: reused, which is what tells a cache hit from a transfer.
    fetched: int
    train: int
    val: int
    holdout: int
    #: Members with no answer: reviewed and nothing applicable.
    skipped: int
    #: Members left out because a feature they carry is under dispute.
    disputed: int = 0


def materialise(request: MaterialiseRequest, context: Context) -> MaterialiseRecord:
    specs = [FeatureSpec.from_dict(raw) for raw in request.features]
    result = ensure_materialised(
        context.catalog,
        request.dataset_id,
        context.datasets_dir,
        features=specs,
        on_progress=context.on_progress,
        cache=context.cache,
    )
    manifest = result.manifest
    if context.on_progress is not None:
        # One last tick, whichever way the version was obtained, so a caller
        # watching ticks learns that materialising is over. docs/adr/0031
        context.on_progress(len(manifest.samples), len(manifest.samples))
    return MaterialiseRecord(
        directory=result.directory,
        dataset=manifest.dataset,
        version=manifest.version or 0,
        fetched=result.fetched,
        train=len(manifest.train),
        val=len(manifest.val),
        holdout=len(manifest.holdout),
        skipped=sum(1 for s in manifest.samples if s.value is None),
        disputed=len(manifest.disputed),
    )


# ----------------------------------------------------------------------
# split
# ----------------------------------------------------------------------


class SplitRequest(Strict):
    """Which side each sample is on, read from the version or drawn afresh.

    Without a seed the sides are the ones the catalog's version carries.
    With one, the sides are drawn again from nothing and written into a
    copy of the directory; the version on disk is never rewritten.
    ``group_by`` names the metadata key whose values stay on one side in
    that draw, which need not be the key the version was frozen under. See
    ``docs/adr/0024``.
    """

    dataset_dir: Path
    seed: int | None = None
    val_ratio: float = 0.2
    holdout_ratio: float = 0.0
    group_by: str | None = None


class SplitRecord(Strict):
    """Where the sides are, and how many samples landed on each.

    The realisation itself — which sample landed where — is not here. It
    is the manifest in ``directory``. A record names what it made and never
    embeds it. See ``docs/adr/0030``.
    """

    directory: Path
    drawn: bool
    seed: int | None
    #: The key the sides respect: the version's own when inherited, the
    #: request's when drawn.
    group_by: str | None
    counts: dict[Side, int]


def split(request: SplitRequest, context: Context) -> SplitRecord:
    source = Path(request.dataset_dir)
    manifest = Manifest.model_validate_json((source / MANIFEST_NAME).read_text())

    if request.seed is None:
        return SplitRecord(
            directory=source,
            drawn=False,
            seed=None,
            group_by=manifest.group_by,
            counts=_counts(manifest),
        )

    key = request.group_by
    members = {
        i: (None if key is None or (value := sample.metadata.get(key)) is None else str(value))
        for i, sample in enumerate(manifest.samples)
    }
    assigned, achieved = assign(
        members,
        val_ratio=request.val_ratio,
        holdout_ratio=request.holdout_ratio,
        seed=request.seed,
    )
    del achieved  # recomputed from the sides by apply_sides, the same way
    # Beside the version, named for the draw. docs/adr/0024
    tag = short_hash(
        {
            "seed": request.seed,
            "val": request.val_ratio,
            "holdout": request.holdout_ratio,
            "group_by": key,
        },
        length=12,
    )
    directory, drawn = apply_sides(
        source,
        manifest,
        [assigned[i] for i in range(len(manifest.samples))],
        val_ratio=request.val_ratio,
        holdout_ratio=request.holdout_ratio,
        tag=tag,
        group_by=key,
    )
    return SplitRecord(
        directory=directory, drawn=True, seed=request.seed, group_by=key, counts=_counts(drawn)
    )


def apply_sides(
    source: Path,
    manifest: Manifest,
    sides: list[str],
    *,
    val_ratio: float | None,
    holdout_ratio: float | None,
    tag: str,
    group_by: str | None = None,
) -> tuple[Path, Manifest]:
    """A copy of ``source`` whose manifest puts each sample on the side given, by position.

    Used by ``split`` for a draw it made and by the modelling host for a
    draw a caller sent. Beside the version, named for ``tag``; the version
    on disk is never rewritten. ``group_by`` is what the copy's manifest
    says its sides respect. See ``docs/adr/0024``.
    """
    if len(sides) != len(manifest.samples):
        raise CatalogError(
            f"{len(sides)} side(s) for a manifest of {len(manifest.samples)} sample(s)."
        )
    samples = [
        sample.model_copy(update={"split": side})
        for sample, side in zip(manifest.samples, sides, strict=True)
    ]
    counts = _counts_of(samples)
    total = len(samples) or 1
    rewritten = manifest.model_copy(
        update={
            "samples": samples,
            "val_ratio": val_ratio,
            "val_ratio_achieved": counts[VAL] / total,
            "holdout_ratio": holdout_ratio,
            "holdout_ratio_achieved": counts[HOLDOUT] / total,
            "group_by": group_by,
        }
    )
    directory = source.parent / f"{source.name}-split-{tag}"
    if not (directory / MANIFEST_NAME).exists():
        for sample in samples:
            target = directory / sample.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                link_or_copy(source / sample.path, target)
        (directory / MANIFEST_NAME).write_text(rewritten.model_dump_json(indent=2))
    return directory, rewritten


def _counts(manifest: Manifest) -> dict[Side, int]:
    return _counts_of(manifest.samples)


def _counts_of(samples) -> dict[Side, int]:
    counts: dict[Side, int] = {TRAIN: 0, VAL: 0, HOLDOUT: 0}
    for sample in samples:
        counts[sample.split] += 1
    return counts


STAGES = (
    Stage("dataset", "1", (), DATASET_VERSION, dataset),
    Stage("materialise", "1", (DATASET_VERSION,), DATASET_DIR, materialise),
    Stage("split", "1", (DATASET_DIR,), DATASET_DIR, split),
)

__all__ = [
    "DATASET_DIR",
    "DATASET_VERSION",
    "STAGES",
    "Context",
    "DatasetRecord",
    "DatasetRequest",
    "MaterialiseRecord",
    "MaterialiseRequest",
    "SplitRecord",
    "SplitRequest",
    "dataset",
    "materialise",
    "split",
]
