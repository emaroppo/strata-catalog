"""Ingest, annotations, queries, datasets and materialising."""

import json

import pytest

from strata.catalog import EVERYTHING, CatalogError, SplitError
from strata.labels import Choices, ClassificationSchema, Manifest, SchemaError

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


def test_ingest_records_a_grouping_as_metadata(catalog, files, label_set):
    # A grouping is a key like any other; nothing about it is special
    # until a version is frozen naming it
    catalog.ingest(files(3), media="image", subtype="frames", metadata={"video": "vid1"})
    rows = catalog.samples.unlabelled(label_set, EVERYTHING)
    assert {s.metadata["video"] for s in rows} == {"vid1"}


def test_a_standalone_sample_carries_no_grouping(catalog, files, label_set):
    catalog.ingest(files(3), media="image")
    assert all(not s.metadata for s in catalog.samples.unlabelled(label_set, EVERYTHING))


def test_the_bytes_come_back(catalog, files):
    [sample_id] = catalog.ingest(files(1), media="image")
    label_set_id = catalog.label_sets.create("x", ClassificationSchema())
    [row] = [s for s in catalog.samples.unlabelled(label_set_id, EVERYTHING) if s.id == sample_id]
    assert catalog.blobs.get(row.location) == b"contents of img0"


# ----------------------------------------------------------------------
# Label sets and annotations
# ----------------------------------------------------------------------


def test_a_label_set_round_trips_its_schema(catalog):
    schema = ClassificationSchema(classes=["a", "b"], multiple=False)
    catalog.label_sets.create("single", schema)
    _, restored = catalog.label_sets.get("single")
    assert restored == schema


def test_an_unknown_label_set_is_an_error(catalog):
    with pytest.raises(CatalogError, match="No label set"):
        catalog.label_sets.get("nope")


def test_annotating_stores_the_value(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(sample_id, label_set, Choices(values=["cat"]))
    assert catalog.annotations.annotation_of(sample_id, label_set) == Choices(values=["cat"])


def test_an_annotation_is_validated_against_its_schema(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    with pytest.raises(SchemaError, match="fish"):
        catalog.annotations.annotate(sample_id, label_set, Choices(values=["fish"]))


def test_annotating_twice_replaces_rather_than_duplicates(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(sample_id, label_set, Choices(values=["cat"]))
    catalog.annotations.annotate(sample_id, label_set, Choices(values=["dog"]))
    assert catalog.annotations.annotation_of(sample_id, label_set) == Choices(values=["dog"])


def test_an_empty_annotation_is_a_real_answer(catalog, files, label_set):
    # A human looked and found nothing. Distinct from nobody having looked,
    # which is the absence of a row
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(sample_id, label_set, Choices())
    assert catalog.annotations.annotation_of(sample_id, label_set) == Choices()
    assert catalog.samples.unlabelled(label_set, EVERYTHING) == []


def test_a_skipped_sample_has_no_value(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotations.skip(sample_id, label_set)
    assert catalog.annotations.annotation_of(sample_id, label_set) is None


def test_a_skipped_sample_leaves_the_review_queue(catalog, files, label_set):
    ids = catalog.ingest(files(3), media="image")
    catalog.annotations.skip(ids[0], label_set)
    assert {s.id for s in catalog.samples.unlabelled(label_set, EVERYTHING)} == set(ids[1:])


def test_a_skipped_sample_is_not_training_data(catalog, files, label_set):
    ids = catalog.ingest(files(3), media="image")
    catalog.annotations.skip(ids[0], label_set)
    catalog.annotations.annotate(ids[1], label_set, Choices(values=["cat"]))
    assert {s.id for s in catalog.samples.labelled(label_set, EVERYTHING)} == {ids[1]}


# ----------------------------------------------------------------------
# Queries
# ----------------------------------------------------------------------


def test_unlabelled_is_per_label_set(catalog, files):
    # A sample can be classified and still waiting for boxes
    ids = catalog.ingest(files(3), media="image")
    presence = catalog.label_sets.create("presence", ClassificationSchema(classes=["cat"]))
    other = catalog.label_sets.create("other", ClassificationSchema(classes=["cat"]))
    catalog.annotations.annotate(ids[0], presence, Choices(values=["cat"]))

    assert len(catalog.samples.unlabelled(presence, EVERYTHING)) == 2
    assert len(catalog.samples.unlabelled(other, EVERYTHING)) == 3


def test_unlabelled_honours_a_limit(catalog, files, label_set):
    catalog.ingest(files(10), media="image")
    assert len(catalog.samples.unlabelled(label_set, EVERYTHING, limit=4)) == 4


def test_with_class_finds_every_sample_asserting_it(catalog, files, label_set):
    ids = catalog.ingest(files(4), media="image")
    catalog.annotations.annotate(ids[0], label_set, Choices(values=["cat"]))
    catalog.annotations.annotate(ids[1], label_set, Choices(values=["cat", "dog"]))
    catalog.annotations.annotate(ids[2], label_set, Choices(values=["dog"]))

    assert {s.id for s in catalog.samples.with_class(label_set, "cat", EVERYTHING)} == {
        ids[0],
        ids[1],
    }


def test_the_class_index_follows_a_correction(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(sample_id, label_set, Choices(values=["cat"]))
    catalog.annotations.annotate(sample_id, label_set, Choices(values=["dog"]))

    assert catalog.samples.with_class(label_set, "cat", EVERYTHING) == []
    assert len(catalog.samples.with_class(label_set, "dog", EVERYTHING)) == 1


def test_skipping_clears_the_class_index(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(sample_id, label_set, Choices(values=["cat"]))
    catalog.annotations.skip(sample_id, label_set)
    assert catalog.samples.with_class(label_set, "cat", EVERYTHING) == []


# ----------------------------------------------------------------------
# Datasets
# ----------------------------------------------------------------------


def annotate_all(catalog, ids, label_set):
    for i in ids:
        catalog.annotations.annotate(i, label_set, Choices(values=["cat"]))


def test_a_dataset_defaults_to_everything_labelled(catalog, files, label_set):
    ids = catalog.ingest(files(10), media="image")
    annotate_all(catalog, ids[:6], label_set)
    dataset_id = catalog.create_dataset("d", label_set, collections=EVERYTHING)
    manifest = Manifest.model_validate_json(
        (
            catalog.materialise(dataset_id, catalog.blobs.root.parent / "out") / "manifest.json"
        ).read_text()
    )
    assert len(manifest.samples) == 6


def test_an_unchanged_selection_reuses_its_version(catalog, files, label_set):
    # A version describes a selection, not an attempt at one. A round that
    # crashed after freezing its dataset should retry against the same
    # version rather than mint a second saying exactly the same thing.
    ids = catalog.ingest(files(10), media="image")
    annotate_all(catalog, ids, label_set)
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)
    assert first == catalog.create_dataset("d", label_set, collections=EVERYTHING)


def test_a_changed_selection_makes_a_new_version(catalog, files, label_set):
    ids = catalog.ingest(files(10), media="image")
    annotate_all(catalog, ids[:6], label_set)
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    annotate_all(catalog, ids[6:], label_set)
    assert catalog.create_dataset("d", label_set, collections=EVERYTHING) != first


def test_a_corrected_answer_makes_a_new_version(catalog, files, label_set):
    """A version is samples *and what was said about them*.

    Membership alone was the whole identity test, and correcting a label
    leaves membership untouched — so the round was handed the previous
    version, skipped materialising because that manifest was already on
    disk, and trained on the values the correction had just replaced.
    Nothing raised.
    """
    ids = catalog.ingest(files(10), media="image")
    annotate_all(catalog, ids, label_set)
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    catalog.annotations.annotate_many(label_set, [(ids[0], Choices(values=["dog"]))])

    assert catalog.create_dataset("d", label_set, collections=EVERYTHING) != first


def test_a_guess_becoming_an_answer_makes_a_new_version(catalog, files, label_set):
    """Same value, different source, and that is a change.

    An imported guess and a human answer saying the same thing are not the
    same annotation — the distinction is the only thing separating a
    regex's output from a reviewed label, and a frozen version that
    conflates them cannot say which it trained on.
    """
    ids = catalog.ingest(files(4), media="image")
    catalog.annotations.annotate_many(
        label_set, [(i, Choices(values=["cat"])) for i in ids], source="import"
    )
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    catalog.annotations.annotate_many(
        label_set, [(ids[0], Choices(values=["cat"]))], source="human"
    )

    assert catalog.create_dataset("d", label_set, collections=EVERYTHING) != first


def test_a_version_frozen_before_digests_existed_is_not_reused(catalog, files, label_set):
    """Null is unknown, and unknown is not a match.

    A version written before this column cannot say which answers it holds,
    so it cannot claim to hold these. The cost is one extra version per
    project on upgrade, which is visible in `report`; the alternative is
    the silent staleness this exists to stop.
    """
    from sqlalchemy import update

    from strata.catalog.index import tables as t

    ids = catalog.ingest(files(4), media="image")
    annotate_all(catalog, ids, label_set)
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)
    with catalog.engine.begin() as conn:
        conn.execute(update(t.dataset).values(annotation_digest=None))

    assert catalog.create_dataset("d", label_set, collections=EVERYTHING) != first


def test_the_digest_does_not_depend_on_the_order_asked_for(catalog, files, label_set):
    ids = catalog.ingest(files(6), media="image")
    annotate_all(catalog, ids, label_set)
    first = catalog.create_dataset("d", label_set, sample_ids=ids)

    assert catalog.create_dataset("d", label_set, sample_ids=list(reversed(ids))) == first


def test_a_dataset_with_nothing_labelled_is_an_error(catalog, files, label_set):
    catalog.ingest(files(3), media="image")
    with pytest.raises(CatalogError, match="No labelled samples"):
        catalog.create_dataset("d", label_set, collections=EVERYTHING)


def test_the_split_is_inherited_across_versions(catalog, files, label_set, tmp_path):
    ids = catalog.ingest(files(20), media="image")
    annotate_all(catalog, ids[:15], label_set)
    first = catalog.materialise(
        catalog.create_dataset("d", label_set, collections=EVERYTHING), tmp_path / "v1"
    )
    before = {s.id: s.split for s in _manifest(first).samples}

    annotate_all(catalog, ids[15:], label_set)
    second = catalog.materialise(
        catalog.create_dataset("d", label_set, collections=EVERYTHING), tmp_path / "v2"
    )
    after = {s.id: s.split for s in _manifest(second).samples}

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
    return catalog.materialise(
        catalog.create_dataset("d", label_set, collections=EVERYTHING), tmp_path / "out"
    )


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


def test_a_manifest_says_whether_each_label_was_reviewed(catalog, files, label_set, tmp_path):
    """Imports are trained on either way; the record is what makes a poor result readable."""
    ids = catalog.ingest(files(4), media="image")
    catalog.annotations.annotate_many(
        label_set, [(i, Choices(values=["cat"])) for i in ids[:2]], source="import"
    )
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids[2:]])
    dataset_id = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    directory = catalog.materialise(dataset_id, tmp_path / "out")

    by_id = {s.id: (s.source, s.reviewed) for s in _manifest(directory).samples}
    assert by_id == {
        ids[0]: ("import", False),
        ids[1]: ("import", False),
        ids[2]: ("human", True),
        ids[3]: ("human", True),
    }


def test_materialising_again_rewrites_only_the_manifest(catalog, materialised):
    stamps = {p: p.stat().st_mtime_ns for p in (materialised / "files").rglob("*")}
    catalog.materialise(1, materialised)
    assert {p: p.stat().st_mtime_ns for p in (materialised / "files").rglob("*")} == stamps


def test_a_manifest_is_json_anyone_can_read(materialised):
    payload = json.loads((materialised / "manifest.json").read_text())
    assert set(payload) == {
        "format",
        "dataset",
        "version",
        "catalog_id",
        "label_set",
        "label_schema",
        "val_ratio",
        "val_ratio_achieved",
        "holdout_ratio",
        "holdout_ratio_achieved",
        "group_by",
        "sides_from_version",
        "given_split",
        "features",
        "samples",
    }


def test_the_manifest_records_what_the_split_actually_achieved(materialised):
    # Grouping can make the target unreachable, and a caller that asked for
    # 20% should be able to find out it got something else
    manifest = _manifest(materialised)
    assert manifest.val_ratio == pytest.approx(0.2)
    assert manifest.val_ratio_achieved == pytest.approx(0.2)


def test_an_unsplittable_ratio_is_recorded_rather_than_hidden(catalog, files, tmp_path):
    label_set = catalog.label_sets.create("v", ClassificationSchema(classes=["cat"]))
    for video in ("vid1", "vid2"):
        ids = catalog.ingest(
            files(5, prefix=video), media="image", subtype="frames", metadata={"video": video}
        )
        for i in ids:
            catalog.annotations.annotate(i, label_set, Choices(values=["cat"]))

    dataset_id = catalog.create_dataset(
        "frames", label_set, collections=EVERYTHING, val_ratio=0.2, group_by="video"
    )
    manifest = _manifest(catalog.materialise(dataset_id, tmp_path / "frames"))
    assert manifest.group_by == "video"

    assert manifest.val_ratio == pytest.approx(0.2)
    assert manifest.val_ratio_achieved == pytest.approx(0.5)


def test_an_unknown_dataset_is_an_error(catalog, tmp_path):
    with pytest.raises(CatalogError, match="No dataset"):
        catalog.materialise(999, tmp_path / "out")


# ----------------------------------------------------------------------
# Re-ingest
# ----------------------------------------------------------------------


def test_re_ingesting_backfills_a_grouping(catalog, files, label_set):
    # A project that ingested as plain images and then corrected its type
    # re-runs, and the metadata the type records has to follow, or the
    # mistake means a rebuild
    paths = files(3)
    catalog.ingest(paths, media="image")
    assert all(not s.metadata for s in catalog.samples.unlabelled(label_set, EVERYTHING))

    catalog.ingest(paths, media="image", subtype="frames", metadata={"video": "vid1"})
    rows = catalog.samples.unlabelled(label_set, EVERYTHING)
    assert {s.metadata["video"] for s in rows} == {"vid1"}


def test_re_ingesting_updates_the_subtype(catalog, files, label_set):
    paths = files(2)
    catalog.ingest(paths, media="image")
    catalog.ingest(paths, media="image", subtype="frames", metadata={"video": "vid1"})
    assert {s.subtype for s in catalog.samples.unlabelled(label_set, EVERYTHING)} == {"frames"}


def test_re_ingesting_does_not_duplicate(catalog, files):
    paths = files(4)
    first = catalog.ingest(paths, media="image")
    assert catalog.ingest(paths, media="image", metadata={"video": "vid1"}) == first


def test_re_ingesting_leaves_annotations_alone(catalog, files, label_set):
    [sample_id] = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(sample_id, label_set, Choices(values=["cat"]))
    catalog.ingest(files(1), media="image", metadata={"video": "vid1"})
    assert catalog.annotations.annotation_of(sample_id, label_set) == Choices(values=["cat"])


def test_regrouping_cannot_disturb_a_dataset_already_built(catalog, files, label_set, tmp_path):
    paths = files(10)
    ids = catalog.ingest(paths, media="image")
    annotate_all(catalog, ids, label_set)
    first = catalog.materialise(
        catalog.create_dataset("d", label_set, collections=EVERYTHING), tmp_path / "v1"
    )
    before = {s.id: s.split for s in _manifest(first).samples}

    # Membership is materialised, so a later regroup is invisible to a
    # version that already exists
    catalog.ingest(paths, media="image", subtype="frames", metadata={"video": "vid1"})
    after = {s.id: s.split for s in _manifest(first).samples}
    assert after == before


def test_a_grouping_is_respected_only_when_a_version_asks(catalog, files, label_set, tmp_path):
    # Two videos of five frames. Grouped by video, a 20% val can only be
    # half; ungrouped, the same samples split frame by frame
    for video in ("vid1", "vid2"):
        ids = catalog.ingest(files(5, prefix=video), media="image", metadata={"video": video})
        annotate_all(catalog, ids, label_set)

    grouped = catalog.create_dataset("g", label_set, collections=EVERYTHING, group_by="video")
    loose = catalog.create_dataset("u", label_set, collections=EVERYTHING)

    by_video = _manifest(catalog.materialise(grouped, tmp_path / "g"))
    assert by_video.val_ratio_achieved == pytest.approx(0.5)
    sides = {}
    for sample in by_video.samples:
        sides.setdefault(sample.metadata["video"], set()).add(sample.split)
    assert all(len(v) == 1 for v in sides.values())

    frame_by_frame = _manifest(catalog.materialise(loose, tmp_path / "u"))
    assert frame_by_frame.group_by is None
    assert frame_by_frame.val_ratio_achieved == pytest.approx(0.2)


def test_a_version_says_which_version_its_sides_began_at(catalog, files, label_set, tmp_path):
    ids = catalog.ingest(files(10), media="image")
    annotate_all(catalog, ids, label_set)
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)
    more = catalog.ingest(files(5, prefix="more"), media="image")
    annotate_all(catalog, more, label_set)
    second = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    # Inherited sides descend from the first version, and say so
    v1 = _manifest(catalog.materialise(first, tmp_path / "v1"))
    v2 = _manifest(catalog.materialise(second, tmp_path / "v2"))
    assert (v1.version, v1.sides_from_version) == (1, 1)
    assert (v2.version, v2.sides_from_version) == (2, 1)
    kept = {s.id: s.split for s in v1.samples}
    assert all(s.split == kept[s.id] for s in v2.samples if s.id in kept)


def test_a_re_split_starts_a_lineage_of_its_own(catalog, files, label_set, tmp_path):
    ids = catalog.ingest(files(20), media="image")
    annotate_all(catalog, ids, label_set)
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)
    v1 = _manifest(catalog.materialise(first, tmp_path / "v1"))

    # Drawn from nothing under another seed: the sides need not agree with
    # the version before, and the version names itself as where they began
    third = catalog.create_dataset("d", label_set, collections=EVERYTHING, seed=7, inherit=False)
    v2 = _manifest(catalog.materialise(third, tmp_path / "v2"))
    assert (v2.version, v2.sides_from_version) == (2, 2)
    before = {s.id: s.split for s in v1.samples}
    assert any(s.split != before[s.id] for s in v2.samples)

    # Asked again unchanged, the re-split is the version already there;
    # under another seed it is another draw, so another version
    again = catalog.create_dataset("d", label_set, collections=EVERYTHING, seed=7, inherit=False)
    assert again == third
    other = catalog.create_dataset("d", label_set, collections=EVERYTHING, seed=8, inherit=False)
    assert other != third

    # What comes after inherits from the re-split, not from before it
    more = catalog.ingest(files(5, prefix="more"), media="image")
    annotate_all(catalog, more, label_set)
    v4 = _manifest(
        catalog.materialise(
            catalog.create_dataset("d", label_set, collections=EVERYTHING), tmp_path / "v4"
        )
    )
    assert (v4.version, v4.sides_from_version) == (4, 3)


# ----------------------------------------------------------------------
# A split the corpus arrived with
# ----------------------------------------------------------------------


def _benchmark(catalog, files, label_set, sets):
    """Samples carrying the set a public dataset put them in."""
    ids = []
    for name, count in sets:
        batch = catalog.ingest(files(count, prefix=name), media="image", metadata={"bench": name})
        annotate_all(catalog, batch, label_set)
        ids.append(batch)
    return ids


def test_a_given_split_fixes_the_sides_it_names_and_draws_the_rest(
    catalog, files, label_set, tmp_path
):
    from strata.catalog import GivenSplit

    train, test = _benchmark(catalog, files, label_set, [("train", 20), ("test", 5)])
    given = GivenSplit(key="bench", holdout=["test"])
    dataset_id = catalog.create_dataset(
        "d", label_set, collections=EVERYTHING, val_ratio=0.2, given=given
    )
    manifest = _manifest(catalog.materialise(dataset_id, tmp_path / "d"))

    # The benchmark's test set is the holdout, exactly; validation is drawn
    # from its train set since it has no dev; nothing else is held out
    assert {s.id for s in manifest.holdout} == set(test)
    assert {s.id for s in manifest.val} <= set(train)
    assert len(manifest.val) == 5
    assert manifest.given_split == {"key": "bench", "holdout": ["test"], "val": []}


def test_a_given_dev_set_is_the_validation(catalog, files, label_set, tmp_path):
    from strata.catalog import GivenSplit

    train, dev, test = _benchmark(
        catalog, files, label_set, [("train", 12), ("dev", 4), ("test", 4)]
    )
    given = GivenSplit(key="bench", holdout=["test"], val=["dev"])
    dataset_id = catalog.create_dataset("d", label_set, collections=EVERYTHING, given=given)
    manifest = _manifest(catalog.materialise(dataset_id, tmp_path / "d"))

    assert {s.id for s in manifest.val} == set(dev)
    assert {s.id for s in manifest.holdout} == set(test)
    assert {s.id for s in manifest.train} == set(train)


def test_a_given_split_that_contradicts_an_inherited_side_is_refused(catalog, files, label_set):
    from strata.catalog import GivenSplit

    _benchmark(catalog, files, label_set, [("train", 20), ("test", 5)])
    # Frozen once without the benchmark's division: its test samples land
    # wherever the draw put them, most of them in train
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)
    given = GivenSplit(key="bench", holdout=["test"])
    with pytest.raises(CatalogError, match="inherit=False"):
        catalog.create_dataset("d", label_set, collections=EVERYTHING, given=given)
    # Said outright, the version re-splits and starts a lineage of its own
    second = catalog.create_dataset(
        "d", label_set, collections=EVERYTHING, given=given, inherit=False
    )
    assert second != first


def test_a_given_split_wins_over_grouping_and_says_so(catalog, files, label_set, tmp_path):
    from strata.catalog import GivenSplit
    from strata.catalog.stages import Context, DatasetRequest, dataset

    # Two videos of four frames, and a benchmark that put one frame of each
    # in its test set: matching it means cutting both groups
    for video in ("v1", "v2"):
        for name, count in (("train", 3), ("test", 1)):
            ids = catalog.ingest(
                files(count, prefix=f"{video}{name}"),
                media="image",
                metadata={"video": video, "bench": name},
            )
            annotate_all(catalog, ids, label_set)
    record = dataset(
        DatasetRequest(
            name="d",
            label_set=label_set_name(catalog, label_set),
            collections=[EVERYTHING],
            group_by="video",
            given=GivenSplit(key="bench", holdout=["test"]),
        ),
        Context(catalog, tmp_path / "datasets"),
    )
    assert (record.given, record.groups_cut) == (2, 2)
    manifest = _manifest(catalog.materialise(record.dataset_id, tmp_path / "d"))
    assert len(manifest.holdout) == 2


def label_set_name(catalog, label_set_id: int) -> str:
    from sqlalchemy import select

    from strata.catalog.index import tables as t

    with catalog.engine.connect() as conn:
        return conn.execute(
            select(t.label_set.c.name).where(t.label_set.c.id == label_set_id)
        ).scalar_one()


def test_a_version_grouped_differently_is_another_version(catalog, files, label_set):
    ids = catalog.ingest(files(6), media="image", metadata={"video": "one"})
    annotate_all(catalog, ids, label_set)
    # Same samples, same answers, another grouping: not the same freeze
    first = catalog.create_dataset("d", label_set, collections=EVERYTHING)
    assert catalog.create_dataset("d", label_set, collections=EVERYTHING) == first
    with pytest.raises(SplitError):
        # One video is one group, and a group cannot be split
        catalog.create_dataset("d", label_set, collections=EVERYTHING, group_by="video")


def test_materialising_shares_inodes_with_the_blobs(materialised, catalog):
    # A blob is immutable and content-addressed, and a version is derived
    # from it — so a dataset version should cost no disk. Without this every
    # round copies itself and a real project runs out.
    import os

    sample = _manifest(materialised).samples[0]
    row = next(s for s in catalog.samples.labelled(1, EVERYTHING) if s.id == sample.id)
    assert (materialised / sample.path).stat().st_ino == (
        catalog.blobs.path_for(row.location).stat().st_ino
    )
    assert os.stat(materialised / sample.path).st_nlink >= 2


def test_a_materialised_file_still_has_the_right_bytes(materialised):
    sample = _manifest(materialised).samples[0]
    assert (materialised / sample.path).read_bytes().startswith(b"contents of img")


def test_skipped_samples_can_be_listed(catalog, files, label_set):
    ids = catalog.ingest(files(4), media="image")
    catalog.annotations.skip(ids[0], label_set)
    catalog.annotations.skip(ids[1], label_set)
    catalog.annotations.annotate(ids[2], label_set, Choices(values=["cat"]))
    # They belong to neither the labelled set nor the queue, so anything
    # reconstructing the whole picture needs them named
    assert {s.id for s in catalog.samples.skipped(label_set, EVERYTHING)} == {ids[0], ids[1]}


def test_the_three_states_partition_the_catalog(catalog, files, label_set):
    ids = catalog.ingest(files(6), media="image")
    catalog.annotations.skip(ids[0], label_set)
    catalog.annotations.annotate(ids[1], label_set, Choices(values=["cat"]))
    counts = (
        len(catalog.samples.labelled(label_set, EVERYTHING)),
        len(catalog.samples.skipped(label_set, EVERYTHING)),
        len(catalog.samples.unlabelled(label_set, EVERYTHING)),
    )
    assert counts == (1, 1, 4)
    assert sum(counts) == len(ids)


# ----------------------------------------------------------------------
# Collections
# ----------------------------------------------------------------------


def test_a_query_must_say_what_it_draws_from(catalog, files, label_set):
    catalog.ingest(files(3), media="image", collections=["a"])
    # Forgetting to scope is how one project's queue fills with another's
    # data, so it cannot be forgotten
    with pytest.raises(TypeError):
        catalog.samples.unlabelled(label_set)


def test_an_empty_list_is_refused_rather_than_guessed(catalog, files, label_set):
    catalog.ingest(files(3), media="image", collections=["a"])
    with pytest.raises(CatalogError, match="EVERYTHING"):
        catalog.samples.unlabelled(label_set, [])


def test_the_queue_is_scoped(catalog, files, label_set):
    catalog.ingest(files(3, prefix="a"), media="image", collections=["a"])
    catalog.ingest(files(5, prefix="b"), media="image", collections=["b"])
    assert len(catalog.samples.unlabelled(label_set, ["a"])) == 3
    assert len(catalog.samples.unlabelled(label_set, ["b"])) == 5
    assert len(catalog.samples.unlabelled(label_set, EVERYTHING)) == 8


def test_training_data_is_scoped_too(catalog, files, label_set):
    ids = catalog.ingest(files(3, prefix="a"), media="image", collections=["a"])
    other = catalog.ingest(files(2, prefix="b"), media="image", collections=["b"])
    annotate_all(catalog, ids + other, label_set)
    # Dropping a collection declares that data out of scope, training
    # included: quietly carrying it would move the metrics as well as the
    # model, and neither would say why
    assert len(catalog.samples.labelled(label_set, ["a"])) == 3
    assert len(catalog.samples.labelled(label_set, EVERYTHING)) == 5


def test_selecting_a_collection_selects_what_is_under_it(catalog, files, label_set):
    catalog.ingest(files(2, prefix="x"), media="image", collections=["sat/2024"])
    catalog.ingest(files(3, prefix="y"), media="image", collections=["sat/2025"])
    assert len(catalog.samples.unlabelled(label_set, ["sat"])) == 5
    assert len(catalog.samples.unlabelled(label_set, ["sat/2024"])) == 2


def test_a_prefix_does_not_swallow_a_sibling(catalog, files, label_set):
    catalog.ingest(files(2, prefix="x"), media="image", collections=["sat"])
    catalog.ingest(files(3, prefix="y"), media="image", collections=["sat_old"])
    # A bare LIKE 'sat%' would take both, which is the whole reason the
    # match is spelled out
    assert len(catalog.samples.unlabelled(label_set, ["sat"])) == 2


def test_a_sample_can_belong_to_several_collections(catalog, files, label_set):
    paths = files(4)
    catalog.ingest(paths, media="image", collections=["first"])
    catalog.ingest(paths, media="image", collections=["second"])
    # Added rather than replaced: the same images feeding two jobs is what
    # makes a corpus worth keeping
    assert len(catalog.samples.unlabelled(label_set, ["first"])) == 4
    assert len(catalog.samples.unlabelled(label_set, ["second"])) == 4
    assert len(catalog.samples.unlabelled(label_set, EVERYTHING)) == 4


def test_a_sample_in_several_collections_appears_once(catalog, files, label_set):
    paths = files(3)
    catalog.ingest(paths, media="image", collections=["a"])
    catalog.ingest(paths, media="image", collections=["b"])
    # A join would return it once per membership; nothing downstream expects
    # a queue with the same sample three times in it
    assert len(catalog.samples.unlabelled(label_set, ["a", "b"])) == 3


def test_a_sample_in_no_collection_is_reachable_only_deliberately(catalog, files, label_set):
    catalog.ingest(files(3), media="image")
    assert catalog.samples.unlabelled(label_set, ["anything"]) == []
    assert len(catalog.samples.unlabelled(label_set, EVERYTHING)) == 3


def test_a_dataset_must_be_told_where_to_draw_from(catalog, files, label_set):
    ids = catalog.ingest(files(4), media="image", collections=["a"])
    annotate_all(catalog, ids, label_set)
    with pytest.raises(CatalogError, match="collections"):
        catalog.create_dataset("d", label_set)


def test_a_sample_row_can_be_hashed_even_carrying_metadata():
    """A frozen dataclass hashes every field it compares.

    metadata is a dict, so including it made any row describing where it
    came from unusable as a key or set member — which stayed invisible only
    while every sample had none, and then broke a push after three and a
    half minutes of inference.
    """
    from strata.catalog import Location, SampleRow

    row = SampleRow(
        id=1,
        checksum="a" * 64,
        location=Location("x", 0, 1),
        media="image",
        subtype="plain",
        metadata={"source_path": "/raw/img.jpg"},
    )
    assert hash(row)
    assert {row: "kept"}[row] == "kept"


def test_two_rows_for_one_sample_are_the_same_sample():
    from strata.catalog import Location, SampleRow

    common = dict(
        id=1,
        checksum="a" * 64,
        location=Location("x", 0, 1),
        media="image",
        subtype="plain",
    )
    # What is recorded about where a sample came from does not make it a
    # different sample
    assert SampleRow(**common, metadata={"a": 1}) == SampleRow(**common, metadata=None)


def test_a_label_set_keeps_whatever_kind_of_schema_it_is(tmp_path):
    """A catalog holds annotations, not one task's annotations.

    Reading a label set back as classification meant a catalog could store
    boxes it could never hand over — and the mismatch surfaced wherever the
    schema was next used, not where it was made.
    """
    from strata.catalog import Catalog
    from strata.labels import BBoxSchema, ClassificationSchema, SpanSchema

    catalog = Catalog.local(tmp_path / "catalog")
    for name, schema in [
        ("boxes", BBoxSchema(classes=["cat"])),
        ("spans", SpanSchema(classes=["name"])),
        ("choices", ClassificationSchema(classes=["a"])),
    ]:
        catalog.label_sets.create(name, schema)
        assert catalog.label_sets.get(name)[1] == schema


def test_discard_removes_one_source_and_returns_samples_to_the_queue(catalog, files):
    """An unreviewed import is not an answer, and should not read as one."""
    label_set_id = catalog.label_sets.create("x", ClassificationSchema(classes=["cat"]))
    ids = catalog.ingest(files(2), media="image")
    catalog.annotations.annotate(ids[0], label_set_id, Choices(values=["cat"]), source="import")
    catalog.annotations.annotate(ids[1], label_set_id, Choices(values=["cat"]), source="human")

    assert catalog.annotations.discard(label_set_id, "import") == 1

    # The imported one is unlabelled again, the answered one untouched
    assert [row.id for row in catalog.samples.unlabelled(label_set_id, EVERYTHING)] == [ids[0]]
    assert [row.id for row in catalog.samples.labelled(label_set_id, EVERYTHING)] == [ids[1]]
    assert catalog.annotations.annotation_of(ids[0], label_set_id) is None


def test_discard_clears_the_class_index_too(catalog, files):
    """Otherwise a discarded row still answers 'which samples have a cat'."""
    label_set_id = catalog.label_sets.create("x", ClassificationSchema(classes=["cat"]))
    ids = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(ids[0], label_set_id, Choices(values=["cat"]), source="import")
    assert catalog.samples.with_class(label_set_id, "cat", EVERYTHING)

    catalog.annotations.discard(label_set_id, "import")
    assert catalog.samples.with_class(label_set_id, "cat", EVERYTHING) == []


def test_discard_names_a_source_that_is_not_there(catalog):
    label_set_id = catalog.label_sets.create("x", ClassificationSchema(classes=["cat"]))
    assert catalog.annotations.discard(label_set_id, "nobody") == 0


# ----------------------------------------------------------------------
# Which source may replace which
# ----------------------------------------------------------------------


def test_an_import_does_not_replace_a_persons_answer(catalog, files, label_set):
    """Re-running an import after a review pass must not undo the review."""
    ids = catalog.ingest(files(2), media="image")
    catalog.annotations.annotate(ids[0], label_set, Choices(values=["dog"]))

    written = catalog.annotations.annotate_many(
        label_set, [(i, Choices(values=["cat"])) for i in ids], source="import"
    )

    assert (written.annotated, written.kept) == (1, 1)
    assert catalog.annotations.annotation_of(ids[0], label_set) == Choices(values=["dog"])
    assert catalog.annotations.annotation_of(ids[1], label_set) == Choices(values=["cat"])
    # The class index follows what was kept, not what was offered
    assert [s.id for s in catalog.samples.with_class(label_set, "cat", EVERYTHING)] == [ids[1]]


def test_an_import_does_not_replace_a_persons_skip(catalog, files, label_set):
    """A skip is an answer too: someone looked and found nothing applicable."""
    [sample] = catalog.ingest(files(1), media="image")
    catalog.annotations.skip(sample, label_set)

    assert not catalog.annotations.annotate(
        sample, label_set, Choices(values=["cat"]), source="import"
    )
    assert [s.id for s in catalog.samples.skipped(label_set, EVERYTHING)] == [sample]


def test_a_person_replaces_an_import(catalog, files, label_set):
    [sample] = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(sample, label_set, Choices(values=["cat"]), source="import")

    assert catalog.annotations.annotate(sample, label_set, Choices(values=["dog"]))
    assert catalog.annotations.annotation_of(sample, label_set) == Choices(values=["dog"])


def test_an_import_replaces_an_import(catalog, files, label_set):
    """The same batch landed twice, or a corrected one: equal standing, so the later wins."""
    [sample] = catalog.ingest(files(1), media="image")
    catalog.annotations.annotate(sample, label_set, Choices(values=["cat"]), source="import")

    assert catalog.annotations.annotate(sample, label_set, Choices(values=["dog"]), source="import")
    assert catalog.annotations.annotation_of(sample, label_set) == Choices(values=["dog"])


def test_a_source_nobody_ranked_is_refused(catalog, files, label_set):
    [sample] = catalog.ingest(files(1), media="image")
    with pytest.raises(CatalogError, match="Unknown annotation source"):
        catalog.annotations.annotate(sample, label_set, Choices(values=["cat"]), source="Human")


# ----------------------------------------------------------------------
# The holdout
# ----------------------------------------------------------------------


def _sides(manifest) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {"train": set(), "val": set(), "holdout": set()}
    for sample in manifest.samples:
        out[sample.split].add(sample.id)
    return out


def test_a_version_holds_out_what_it_was_asked_to(catalog, files, label_set, tmp_path):
    ids = catalog.ingest(files(20), media="image")
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids])
    dataset_id = catalog.create_dataset(
        "d", label_set, collections=EVERYTHING, val_ratio=0.2, holdout_ratio=0.1
    )
    manifest = _manifest(catalog.materialise(dataset_id, tmp_path / "out"))

    sides = _sides(manifest)
    assert (len(sides["holdout"]), len(sides["val"]), len(sides["train"])) == (2, 4, 14)
    assert manifest.holdout_ratio == pytest.approx(0.1)
    assert manifest.holdout_ratio_achieved == pytest.approx(0.1)
    assert len(manifest.holdout) == 2


def test_a_holdout_is_inherited_and_only_new_samples_can_join_it(
    catalog, files, label_set, tmp_path
):
    first = catalog.ingest(files(20), media="image")
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in first])
    v1 = catalog.create_dataset(
        "d", label_set, collections=EVERYTHING, val_ratio=0.2, holdout_ratio=0.1
    )
    before = _sides(_manifest(catalog.materialise(v1, tmp_path / "v1")))

    more = catalog.ingest(files(20, prefix="more"), media="image")
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in more])
    v2 = catalog.create_dataset(
        "d", label_set, collections=EVERYTHING, val_ratio=0.2, holdout_ratio=0.1
    )
    after = _sides(_manifest(catalog.materialise(v2, tmp_path / "v2")))

    # Every side an earlier version decided is kept, holdout included
    for side in ("train", "val", "holdout"):
        assert before[side] <= after[side]
    # The new holdout members come from the new samples alone
    assert after["holdout"] - before["holdout"] <= set(more)
    assert len(after["holdout"]) == 4


def test_a_version_of_only_inherited_samples_reports_an_empty_holdout(
    catalog, files, label_set, tmp_path
):
    """Inherited sides are never overruled, so there is nothing to draw from.

    Visible rather than silent: the manifest says what was asked and that
    nothing was achieved. A study wanting a holdout on a dataset frozen
    before there were any draws its own split and records the draw.
    """
    ids = catalog.ingest(files(10), media="image")
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids])
    catalog.create_dataset("d", label_set, collections=EVERYTHING, val_ratio=0.2)
    v2 = catalog.create_dataset(
        "d", label_set, collections=EVERYTHING, val_ratio=0.2, holdout_ratio=0.2
    )
    manifest = _manifest(catalog.materialise(v2, tmp_path / "v2"))
    assert manifest.holdout == []
    assert manifest.holdout_ratio == pytest.approx(0.2)
    assert manifest.holdout_ratio_achieved == 0.0


def test_asking_for_a_holdout_is_a_new_version_even_over_the_same_members(
    catalog, files, label_set
):
    ids = catalog.ingest(files(10), media="image")
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids])
    v1 = catalog.create_dataset("d", label_set, collections=EVERYTHING)
    assert catalog.create_dataset("d", label_set, collections=EVERYTHING) == v1
    assert catalog.create_dataset("d", label_set, collections=EVERYTHING, holdout_ratio=0.2) != v1
