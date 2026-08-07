"""Ingest, annotations, queries, datasets and materialising."""

import json

import pytest

from strata.catalog import CatalogError, Manifest
from strata.labels import Choices, ClassificationSchema, SchemaError

# ----------------------------------------------------------------------
# Ingest
# ----------------------------------------------------------------------


def test_ingest_registers_every_file(catalog, files):
    assert len(catalog.ingest(files(10), media="image")) == 10


def test_ingest_is_idempotent(catalog, files):
    paths = files(5)
    # Re-running over a growing directory has to be safe, or every ingest
    # doubles the catalog
    assert catalog.ingest(paths, media="image") == catalog.ingest(paths, media="image")


def test_identical_bytes_under_two_names_are_one_sample(catalog, tmp_path):
    for name in ("a.jpg", "b.jpg"):
        (tmp_path / name).write_bytes(b"identical")
    ids = catalog.ingest([tmp_path / "a.jpg", tmp_path / "b.jpg"], media="image")
    assert ids[0] == ids[1]


def test_ingest_records_the_group(catalog, files, label_set):
    catalog.ingest(files(3), media="image", subtype="frames", group_id="vid1")
    assert {s.group_id for s in catalog.unlabelled(label_set)} == {"vid1"}


def test_a_standalone_sample_has_no_group(catalog, files, label_set):
    catalog.ingest(files(3), media="image")
    assert {s.group_id for s in catalog.unlabelled(label_set)} == {None}


def test_the_bytes_come_back(catalog, files):
    [sample_id] = catalog.ingest(files(1), media="image")
    [row] = [s for s in catalog.unlabelled(catalog.create_label_set(
        "x", ClassificationSchema())) if s.id == sample_id]
    assert catalog.blobs.get(row.location) == b"contents of img0"


# ----------------------------------------------------------------------
# Label sets and annotations
# ----------------------------------------------------------------------


def test_a_label_set_round_trips_its_schema(catalog):
    schema = ClassificationSchema(classes=["a", "b"], multiple=False)
    catalog.create_label_set("single", schema)
    _, restored = catalog.label_set("single")
    assert restored == schema


def test_an_unknown_label_set_is_an_error(catalog):
    with pytest.raises(CatalogError, match="No label set"):
        catalog.label_set("nope")


def test_annotating_stores_the_value(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotate(sample_id, label_set, Choices(values=["cat"]))
    assert catalog.annotation_of(sample_id, label_set) == Choices(values=["cat"])


def test_an_annotation_is_validated_against_its_schema(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    with pytest.raises(SchemaError, match="fish"):
        catalog.annotate(sample_id, label_set, Choices(values=["fish"]))


def test_annotating_twice_replaces_rather_than_duplicates(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotate(sample_id, label_set, Choices(values=["cat"]))
    catalog.annotate(sample_id, label_set, Choices(values=["dog"]))
    assert catalog.annotation_of(sample_id, label_set) == Choices(values=["dog"])


def test_an_empty_annotation_is_a_real_answer(catalog, files, label_set):
    # A human looked and found nothing. Distinct from nobody having looked,
    # which is the absence of a row
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotate(sample_id, label_set, Choices())
    assert catalog.annotation_of(sample_id, label_set) == Choices()
    assert catalog.unlabelled(label_set) == []


def test_a_skipped_sample_has_no_value(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.skip(sample_id, label_set)
    assert catalog.annotation_of(sample_id, label_set) is None


def test_a_skipped_sample_leaves_the_review_queue(catalog, files, label_set):
    ids = catalog.ingest(files(3), media="image")
    catalog.skip(ids[0], label_set)
    assert {s.id for s in catalog.unlabelled(label_set)} == set(ids[1:])


def test_a_skipped_sample_is_not_training_data(catalog, files, label_set):
    ids = catalog.ingest(files(3), media="image")
    catalog.skip(ids[0], label_set)
    catalog.annotate(ids[1], label_set, Choices(values=["cat"]))
    assert {s.id for s in catalog.labelled(label_set)} == {ids[1]}


# ----------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------


def test_unlabelled_is_per_label_set(catalog, files):
    # A sample can be classified and still waiting for boxes
    ids = catalog.ingest(files(3), media="image")
    presence = catalog.create_label_set("presence", ClassificationSchema(classes=["cat"]))
    other = catalog.create_label_set("other", ClassificationSchema(classes=["cat"]))
    catalog.annotate(ids[0], presence, Choices(values=["cat"]))

    assert len(catalog.unlabelled(presence)) == 2
    assert len(catalog.unlabelled(other)) == 3


def test_unlabelled_honours_a_limit(catalog, files, label_set):
    catalog.ingest(files(10), media="image")
    assert len(catalog.unlabelled(label_set, limit=4)) == 4


def test_with_class_finds_every_sample_asserting_it(catalog, files, label_set):
    ids = catalog.ingest(files(4), media="image")
    catalog.annotate(ids[0], label_set, Choices(values=["cat"]))
    catalog.annotate(ids[1], label_set, Choices(values=["cat", "dog"]))
    catalog.annotate(ids[2], label_set, Choices(values=["dog"]))

    assert {s.id for s in catalog.with_class(label_set, "cat")} == {ids[0], ids[1]}


def test_the_class_index_follows_a_correction(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotate(sample_id, label_set, Choices(values=["cat"]))
    catalog.annotate(sample_id, label_set, Choices(values=["dog"]))

    assert catalog.with_class(label_set, "cat") == []
    assert len(catalog.with_class(label_set, "dog")) == 1


def test_skipping_clears_the_class_index(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotate(sample_id, label_set, Choices(values=["cat"]))
    catalog.skip(sample_id, label_set)
    assert catalog.with_class(label_set, "cat") == []


# ----------------------------------------------------------------------
# Datasets
# ----------------------------------------------------------------------


def annotate_all(catalog, ids, label_set):
    for i in ids:
        catalog.annotate(i, label_set, Choices(values=["cat"]))


def test_a_dataset_defaults_to_everything_labelled(catalog, files, label_set):
    ids = catalog.ingest(files(10), media="image")
    annotate_all(catalog, ids[:6], label_set)
    dataset_id = catalog.create_dataset("d", label_set)
    manifest = Manifest.model_validate_json(
        (catalog.materialise(dataset_id, catalog.blobs.root.parent / "out")
         / "manifest.json").read_text()
    )
    assert len(manifest.samples) == 6


def test_versions_increment(catalog, files, label_set):
    ids = catalog.ingest(files(10), media="image")
    annotate_all(catalog, ids, label_set)
    first = catalog.create_dataset("d", label_set)
    second = catalog.create_dataset("d", label_set)
    assert first != second


def test_a_dataset_with_nothing_labelled_is_an_error(catalog, files, label_set):
    catalog.ingest(files(3), media="image")
    with pytest.raises(CatalogError, match="No labelled samples"):
        catalog.create_dataset("d", label_set)


def test_the_split_is_inherited_across_versions(catalog, files, label_set, tmp_path):
    ids = catalog.ingest(files(20), media="image")
    annotate_all(catalog, ids[:15], label_set)
    first = catalog.materialise(catalog.create_dataset("d", label_set), tmp_path / "v1")
    before = {s.id: s.val for s in _manifest(first).samples}

    annotate_all(catalog, ids[15:], label_set)
    second = catalog.materialise(catalog.create_dataset("d", label_set), tmp_path / "v2")
    after = {s.id: s.val for s in _manifest(second).samples}

    # The whole point: a warm-started model is never scored on a sample an
    # earlier round trained it on
    assert all(after[i] == before[i] for i in before)
    assert len(after) == 20


# ----------------------------------------------------------------------
# Materialising
# ----------------------------------------------------------------------


def _manifest(directory) -> Manifest:
    return Manifest.model_validate_json((directory / "manifest.json").read_text())


@pytest.fixture
def materialised(catalog, files, label_set, tmp_path):
    ids = catalog.ingest(files(10), media="image")
    annotate_all(catalog, ids, label_set)
    return catalog.materialise(catalog.create_dataset("d", label_set), tmp_path / "out")


def test_the_manifest_describes_the_dataset(materialised):
    manifest = _manifest(materialised)
    assert (manifest.dataset, manifest.version, manifest.label_set) == ("d", 1, "presence")


def test_the_manifest_carries_the_schema(materialised):
    # So a trainer needs no database to know what the classes are
    assert _manifest(materialised).label_schema.classes == ["cat", "dog", "bird"]


def test_the_manifest_carries_the_checksums(materialised):
    # Ids are catalog-local; the checksum is what a different catalog can
    # match these samples on
    assert all(len(s.checksum) == 64 for s in _manifest(materialised).samples)


def test_every_manifest_path_resolves(materialised):
    for sample in _manifest(materialised).samples:
        assert (materialised / sample.path).exists()


def test_the_files_are_the_real_bytes(materialised):
    sample = _manifest(materialised).samples[0]
    assert (materialised / sample.path).read_bytes().startswith(b"contents of img")


def test_the_manifest_splits_into_train_and_val(materialised):
    manifest = _manifest(materialised)
    assert len(manifest.val) == 2
    assert len(manifest.train) == 8
    assert len(manifest.train) + len(manifest.val) == len(manifest.samples)


def test_annotations_travel_with_the_files(materialised):
    assert all(s.value == Choices(values=["cat"]) for s in _manifest(materialised).samples)


def test_materialising_again_rewrites_only_the_manifest(catalog, materialised):
    stamps = {p: p.stat().st_mtime_ns for p in (materialised / "files").rglob("*")}
    catalog.materialise(1, materialised)
    assert {p: p.stat().st_mtime_ns for p in (materialised / "files").rglob("*")} == stamps


def test_a_manifest_is_json_anyone_can_read(materialised):
    payload = json.loads((materialised / "manifest.json").read_text())
    assert set(payload) == {
        "dataset",
        "version",
        "label_set",
        "label_schema",
        "val_ratio",
        "val_ratio_achieved",
        "samples",
    }


def test_the_manifest_records_what_the_split_actually_achieved(materialised):
    # Grouping can make the target unreachable, and a caller that asked for
    # 20% should be able to find out it got something else
    manifest = _manifest(materialised)
    assert manifest.val_ratio == pytest.approx(0.2)
    assert manifest.val_ratio_achieved == pytest.approx(0.2)


def test_an_unsplittable_ratio_is_recorded_rather_than_hidden(catalog, files, tmp_path):
    label_set = catalog.create_label_set("v", ClassificationSchema(classes=["cat"]))
    for video in ("vid1", "vid2"):
        ids = catalog.ingest(
            files(5, prefix=video), media="image", subtype="frames", group_id=video
        )
        for i in ids:
            catalog.annotate(i, label_set, Choices(values=["cat"]))

    dataset_id = catalog.create_dataset("frames", label_set, val_ratio=0.2)
    manifest = _manifest(catalog.materialise(dataset_id, tmp_path / "frames"))

    assert manifest.val_ratio == pytest.approx(0.2)
    assert manifest.val_ratio_achieved == pytest.approx(0.5)


def test_an_unknown_dataset_is_an_error(catalog, tmp_path):
    with pytest.raises(CatalogError, match="No dataset"):
        catalog.materialise(999, tmp_path / "out")
