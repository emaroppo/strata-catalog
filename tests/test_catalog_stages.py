"""The catalog's stages: requests in, records out, and the split's two modes."""

import json

import pytest

from strata.catalog import EVERYTHING, CatalogError
from strata.catalog.stages import (
    Context,
    DatasetRequest,
    MaterialiseRequest,
    SplitRequest,
    dataset,
    materialise,
    split,
)
from strata.labels import MANIFEST_NAME, Choices, ClassificationSchema, Manifest


@pytest.fixture
def stocked(catalog, files):
    """Twenty labelled samples in five groups, and a label set over them."""
    label_set = catalog.label_sets.create("presence", ClassificationSchema(classes=["cat"]))
    ids = []
    for group in range(5):
        ids += catalog.ingest(files(4, prefix=f"vid{group}"), media="image", group_id=f"vid{group}")
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids])
    return catalog


@pytest.fixture
def context(stocked, tmp_path) -> Context:
    return Context(stocked, tmp_path / "datasets")


def _request(**overrides) -> DatasetRequest:
    fields = {"name": "d", "label_set": "presence", "collections": [EVERYTHING]}
    return DatasetRequest(**{**fields, **overrides})


def _manifest(directory) -> Manifest:
    return Manifest.model_validate_json((directory / MANIFEST_NAME).read_text())


# ----------------------------------------------------------------------
# dataset
# ----------------------------------------------------------------------


def test_dataset_freezes_a_version_and_says_what_it_is(context):
    record = dataset(_request(holdout_ratio=0.2), context)
    ref = context.catalog.dataset_named(record.dataset_id)
    assert (record.name, record.version) == (ref.name, ref.version) == ("d", 1)
    assert record.annotation_digest == ref.annotation_digest
    assert record.catalog_id == context.catalog.id
    assert record.samples == 20


def test_an_unknown_label_set_is_refused_with_the_way_forward(context):
    with pytest.raises(CatalogError, match="ingest"):
        dataset(_request(label_set="nope"), context)


def test_nothing_labelled_is_refused(catalog, tmp_path):
    catalog.label_sets.create("presence", ClassificationSchema(classes=["cat"]))
    with pytest.raises(CatalogError, match="Nothing is labelled"):
        dataset(_request(), Context(catalog, tmp_path / "datasets"))


def test_a_request_refuses_a_key_it_does_not_know():
    with pytest.raises(ValueError, match="extra"):
        DatasetRequest(name="d", label_set="p", collections=["*"], val_ration=0.3)


# ----------------------------------------------------------------------
# materialise
# ----------------------------------------------------------------------


def test_materialise_writes_the_version_and_counts_its_sides(context):
    frozen = dataset(_request(holdout_ratio=0.2), context)
    ticks = []
    context.on_progress = lambda done, total: ticks.append((done, total))

    built = materialise(MaterialiseRequest(dataset_id=frozen.dataset_id), context)

    assert built.directory == context.datasets_dir / "d" / "v001"
    assert (built.dataset, built.version) == ("d", 1)
    assert (built.train, built.val, built.holdout, built.skipped) == (12, 4, 4, 0)
    assert built.fetched == 20
    # The last tick says it is over, whatever the backend did
    assert ticks[-1] == (20, 20)


def test_a_version_already_on_disk_is_reused_not_refetched(context):
    frozen = dataset(_request(), context)
    first = materialise(MaterialiseRequest(dataset_id=frozen.dataset_id), context)
    again = materialise(MaterialiseRequest(dataset_id=frozen.dataset_id), context)
    assert again.directory == first.directory
    assert again.fetched == 0


# ----------------------------------------------------------------------
# split
# ----------------------------------------------------------------------


@pytest.fixture
def built(context):
    frozen = dataset(_request(holdout_ratio=0.2), context)
    return materialise(MaterialiseRequest(dataset_id=frozen.dataset_id), context)


def test_the_default_reads_the_sides_the_version_carries(built, context):
    record = split(SplitRequest(dataset_dir=built.directory), context)
    assert record.directory == built.directory
    assert not record.drawn and record.seed is None
    assert record.counts == {"train": 12, "val": 4, "holdout": 4}
    manifest = _manifest(built.directory)
    assert set(record.sides["holdout"]) == {s.checksum for s in manifest.holdout}


def test_a_seed_draws_its_own_sides_into_a_copy(built, context):
    request = SplitRequest(dataset_dir=built.directory, seed=3, val_ratio=0.4, holdout_ratio=0.2)
    record = split(request, context)

    assert record.drawn and record.seed == 3
    assert record.directory != built.directory
    assert record.directory.parent == built.directory.parent
    # Group-aware: whole videos, so the counts are multiples of four
    assert record.counts == {"train": 8, "val": 8, "holdout": 4}
    drawn = _manifest(record.directory)
    assert {s.checksum for s in drawn.val} == set(record.sides["val"])
    assert (drawn.val_ratio, drawn.holdout_ratio) == (0.4, 0.2)
    assert all((record.directory / s.path).exists() for s in drawn.samples)
    # The version on disk is untouched: the catalog reuses it by name
    assert _manifest(built.directory).holdout_ratio_achieved == pytest.approx(0.2)
    assert len(_manifest(built.directory).val) == 4


def test_the_same_seed_is_the_same_draw_and_directory(built, context):
    a = split(SplitRequest(dataset_dir=built.directory, seed=3), context)
    b = split(SplitRequest(dataset_dir=built.directory, seed=3), context)
    c = split(SplitRequest(dataset_dir=built.directory, seed=4), context)
    assert a.directory == b.directory and a.sides == b.sides
    assert c.directory != a.directory and c.sides != a.sides


def test_a_drawn_manifest_is_still_a_manifest(built, context):
    record = split(SplitRequest(dataset_dir=built.directory, seed=1), context)
    payload = json.loads((record.directory / MANIFEST_NAME).read_text())
    assert payload["format"] == _manifest(built.directory).format
    assert {s["split"] for s in payload["samples"]} <= {"train", "val", "holdout"}
