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
        ids += catalog.ingest(
            files(4, prefix=f"vid{group}"), media="image", metadata={"video": f"vid{group}"}
        )
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids])
    return catalog


@pytest.fixture
def context(stocked, tmp_path) -> Context:
    return Context(stocked, tmp_path / "datasets")


def _request(**overrides) -> DatasetRequest:
    # Grouped by video, as a frames project would ask
    fields = {
        "name": "d",
        "label_set": "presence",
        "collections": [EVERYTHING],
        "group_by": "video",
    }
    return DatasetRequest(**{**fields, **overrides})


def _manifest(directory) -> Manifest:
    return Manifest.model_validate_json((directory / MANIFEST_NAME).read_text())


# ----------------------------------------------------------------------
# dataset
# ----------------------------------------------------------------------


def test_dataset_freezes_a_version_and_says_what_it_is(context):
    record = dataset(_request(holdout_ratio=0.2), context)
    ref = context.catalog.datasets.named(record.dataset_id)
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
    # The key the version was frozen under, since the sides are its own
    assert record.group_by == "video"
    manifest = _manifest(built.directory)
    assert record.counts["holdout"] == len(manifest.holdout)


def test_a_draw_may_group_by_another_key_or_none(built, context):
    # A directory carries every sample's metadata, so a study can split the
    # same version by video for one trial and frame by frame for another
    by_video = split(SplitRequest(dataset_dir=built.directory, seed=3, group_by="video"), context)
    loose = split(SplitRequest(dataset_dir=built.directory, seed=3), context)
    assert by_video.directory != loose.directory
    assert (by_video.group_by, loose.group_by) == ("video", None)
    assert all(n % 4 == 0 for n in by_video.counts.values())
    assert _manifest(loose.directory).group_by is None


def test_a_seed_draws_its_own_sides_into_a_copy(built, context):
    request = SplitRequest(
        dataset_dir=built.directory, seed=3, val_ratio=0.4, holdout_ratio=0.2, group_by="video"
    )
    record = split(request, context)

    assert record.drawn and record.seed == 3
    assert record.directory != built.directory
    assert record.directory.parent == built.directory.parent
    # Group-aware: whole videos, so the counts are multiples of four
    assert record.counts == {"train": 8, "val": 8, "holdout": 4}
    drawn = _manifest(record.directory)
    assert len(drawn.val) == record.counts["val"]
    assert (drawn.val_ratio, drawn.holdout_ratio) == (0.4, 0.2)
    assert all((record.directory / s.path).exists() for s in drawn.samples)
    # The version on disk is untouched: the catalog reuses it by name
    assert _manifest(built.directory).holdout_ratio_achieved == pytest.approx(0.2)
    assert len(_manifest(built.directory).val) == 4


def test_sides_given_by_position_are_applied_to_a_copy(built):
    # What the modelling host does with a split a caller sent
    from strata.catalog.stages import apply_sides

    manifest = _manifest(built.directory)
    sides = ["holdout"] * 4 + ["train"] * (len(manifest.samples) - 4)
    directory, rewritten = apply_sides(
        built.directory, manifest, sides, val_ratio=0.0, holdout_ratio=0.2, tag="given"
    )
    assert directory == built.directory.parent / f"{built.directory.name}-split-given"
    assert [s.split for s in _manifest(directory).samples] == sides
    assert rewritten.holdout_ratio_achieved == 4 / len(sides)
    assert all((directory / s.path).exists() for s in rewritten.samples)
    # The version itself keeps its sides
    assert [s.split for s in _manifest(built.directory).samples] != sides


def test_the_same_seed_is_the_same_draw_and_directory(built, context):
    a = split(SplitRequest(dataset_dir=built.directory, seed=3, group_by="video"), context)
    b = split(SplitRequest(dataset_dir=built.directory, seed=3, group_by="video"), context)
    c = split(SplitRequest(dataset_dir=built.directory, seed=4, group_by="video"), context)
    assert a.directory == b.directory and a.counts == b.counts
    assert c.directory != a.directory
    sides = lambda record: [s.split for s in _manifest(record.directory).samples]  # noqa: E731
    assert sides(a) == sides(b) and sides(c) != sides(a)


def test_a_drawn_manifest_is_still_a_manifest(built, context):
    record = split(SplitRequest(dataset_dir=built.directory, seed=1, group_by="video"), context)
    payload = json.loads((record.directory / MANIFEST_NAME).read_text())
    assert payload["format"] == _manifest(built.directory).format
    assert {s["split"] for s in payload["samples"]} <= {"train", "val", "holdout"}
