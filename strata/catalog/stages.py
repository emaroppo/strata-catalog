"""The catalog's stages: what an experiment asks of it, as requests and records.

Three of them. ``dataset`` freezes a version, ``materialise`` puts it on
disk, and ``split`` decides — or, by default, reads — which side each
sample is on. Each takes a request and a context and returns a record that
names what it made by identity: a dataset id, a directory. Nothing in a
record is large, so the record can sit in a ledger whole.

The CLI calls these and renders what comes back; the orchestrator calls
them from a config. Neither has a second implementation.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from strata.common.canonical import short_hash
from strata.common.stages import Stage
from strata.labels import MANIFEST_NAME, Manifest

from .catalog import Catalog, CatalogError, _link_or_copy
from .features import FeatureSpec
from .materialised import ensure_materialised
from .split import HOLDOUT, TRAIN, VAL, assign

#: The kinds these stages link, for a chain to be checked before it runs.
DATASET_VERSION = "dataset_version"
DATASET_DIR = "dataset_dir"


class Strict(BaseModel):
    """Unknown keys are an error; a misspelled one would silently do nothing."""

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
    on_progress: object = None


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


class DatasetRecord(Strict):
    """A frozen version, by identity, and what it says about itself."""

    dataset_id: int
    name: str
    version: int
    annotation_digest: str | None
    catalog_id: str
    label_set_id: int
    samples: int


def dataset(request: DatasetRequest, context: Context) -> DatasetRecord:
    catalog = context.catalog
    try:
        label_set_id, _ = catalog.label_set(request.label_set)
    except CatalogError as exc:
        raise CatalogError(
            f"No label set named {request.label_set!r} in the catalog. "
            f"Run 'auto-labeller ingest' first, or set [catalog] label_set."
        ) from exc
    labelled = catalog.labelled(label_set_id, request.collections)
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
    )
    ref = catalog.dataset_named(dataset_id)
    return DatasetRecord(
        dataset_id=dataset_id,
        name=ref.name,
        version=ref.version,
        annotation_digest=ref.annotation_digest,
        catalog_id=catalog.id,
        label_set_id=label_set_id,
        samples=len(labelled),
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
        # One last tick, whichever way the version was obtained. A version
        # already on disk fetches nothing, and a local backend links rather
        # than downloads — so a caller watching ticks would never learn that
        # materialising was over, and would keep saying so while the GPU ran.
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
    )


# ----------------------------------------------------------------------
# split
# ----------------------------------------------------------------------


class SplitRequest(Strict):
    """Which side each sample is on, read from the version or drawn afresh.

    Without a seed the sides are the ones the catalog's version carries —
    the default, and what makes trials comparable. With one, the sides are
    drawn again from nothing, group-aware, and written into a copy of the
    directory; the version on disk is never rewritten, since the catalog
    reuses it by name.
    """

    dataset_dir: Path
    seed: int | None = None
    val_ratio: float = 0.2
    holdout_ratio: float = 0.0


class SplitRecord(Strict):
    """The realisation: which sample landed where, by checksum."""

    directory: Path
    drawn: bool
    seed: int | None
    sides: dict[Literal["train", "val", "holdout"], list[str]]

    @property
    def counts(self) -> dict[str, int]:
        return {side: len(checksums) for side, checksums in self.sides.items()}


def split(request: SplitRequest, context: Context) -> SplitRecord:
    source = Path(request.dataset_dir)
    manifest = Manifest.model_validate_json((source / MANIFEST_NAME).read_text())

    if request.seed is None:
        return SplitRecord(directory=source, drawn=False, seed=None, sides=_sides(manifest))

    members = {i: sample.group_id for i, sample in enumerate(manifest.samples)}
    assigned, achieved = assign(
        members,
        val_ratio=request.val_ratio,
        holdout_ratio=request.holdout_ratio,
        seed=request.seed,
    )
    samples = [
        sample.model_copy(update={"split": assigned[i]})
        for i, sample in enumerate(manifest.samples)
    ]
    drawn = manifest.model_copy(
        update={
            "samples": samples,
            "val_ratio": request.val_ratio,
            "val_ratio_achieved": achieved.val,
            "holdout_ratio": request.holdout_ratio,
            "holdout_ratio_achieved": achieved.holdout,
        }
    )

    # Beside the version, named for the draw, so two seeds are two
    # directories and the same seed is the same one.
    tag = short_hash(
        {"seed": request.seed, "val": request.val_ratio, "holdout": request.holdout_ratio},
        length=12,
    )
    directory = source.parent / f"{source.name}-split-{tag}"
    if not (directory / MANIFEST_NAME).exists():
        for sample in samples:
            target = directory / sample.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                _link_or_copy(source / sample.path, target)
        (directory / MANIFEST_NAME).write_text(drawn.model_dump_json(indent=2))
    return SplitRecord(directory=directory, drawn=True, seed=request.seed, sides=_sides(drawn))


def _sides(manifest: Manifest) -> dict[str, list[str]]:
    sides: dict[str, list[str]] = {TRAIN: [], VAL: [], HOLDOUT: []}
    for sample in manifest.samples:
        sides[sample.split].append(sample.checksum)
    return sides


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
