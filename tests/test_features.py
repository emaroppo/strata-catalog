"""Features reach a model, and a project that declares one gets it."""

import pytest

from strata.catalog import EVERYTHING
from strata.catalog.versions.features import FeatureError, FeatureSpec
from strata.labels import Choices, ClassificationSchema


def test_a_declaration_must_name_its_source():
    with pytest.raises(FeatureError, match="source"):
        FeatureSpec.from_dict({"name": "species", "ref": "x"})


def test_an_unknown_source_is_refused():
    with pytest.raises(FeatureError, match="expected one of"):
        FeatureSpec(name="s", source="somewhere", ref="x")


def test_a_metadata_feature_reaches_the_manifest(catalog, files, label_set, tmp_path):
    paths = files(4)
    ids = catalog.ingest(
        paths, media="image", metadata_for=lambda p: {"species": f"sp-{p.stem[-1]}"}
    )
    for i in ids:
        catalog.annotations.annotate(i, label_set, Choices(values=["cat"]))
    dataset = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    out = catalog.materialise(
        dataset,
        tmp_path / "out",
        features=[FeatureSpec(name="species", source="metadata", ref="species")],
    )
    from strata.labels import Manifest

    manifest = Manifest.model_validate_json((out / "manifest.json").read_text())

    assert [f["name"] for f in manifest.features] == ["species"]
    assert all(s.features["species"].startswith("sp-") for s in manifest.samples)


def test_a_label_set_feature_reaches_the_manifest(catalog, files, label_set, tmp_path):
    """The primary case: one project's target is another's feature."""
    other = catalog.label_sets.create("species", ClassificationSchema(classes=["tomato", "potato"]))
    ids = catalog.ingest(files(4), media="image")
    for n, i in enumerate(ids):
        catalog.annotations.annotate(i, label_set, Choices(values=["cat"]))
        catalog.annotations.annotate(i, other, Choices(values=["tomato" if n % 2 else "potato"]))
    dataset = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    out = catalog.materialise(
        dataset,
        tmp_path / "out",
        features=[FeatureSpec(name="species", source="label_set", ref="species")],
    )
    from strata.labels import Manifest

    manifest = Manifest.model_validate_json((out / "manifest.json").read_text())

    assert {tuple(s.features["species"]) for s in manifest.samples} == {
        ("tomato",),
        ("potato",),
    }


def test_a_sample_the_feature_does_not_cover_is_left_absent(catalog, files, label_set, tmp_path):
    """Not filled in. A zero is an answer; 'not known' is not."""
    other = catalog.label_sets.create("species", ClassificationSchema(classes=["tomato"]))
    ids = catalog.ingest(files(3), media="image")
    for i in ids:
        catalog.annotations.annotate(i, label_set, Choices(values=["cat"]))
    catalog.annotations.annotate(ids[0], other, Choices(values=["tomato"]))
    dataset = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    out = catalog.materialise(
        dataset,
        tmp_path / "out",
        features=[FeatureSpec(name="species", source="label_set", ref="species")],
    )
    from strata.labels import Manifest

    manifest = Manifest.model_validate_json((out / "manifest.json").read_text())
    covered = [s for s in manifest.samples if s.features]

    assert len(covered) == 1
    assert all(s.features == {} for s in manifest.samples if s.id != ids[0])


def test_a_feature_naming_a_missing_label_set_is_refused(catalog, files, label_set, tmp_path):
    ids = catalog.ingest(files(2), media="image")
    for i in ids:
        catalog.annotations.annotate(i, label_set, Choices(values=["cat"]))
    dataset = catalog.create_dataset("d", label_set, collections=EVERYTHING)

    with pytest.raises(FeatureError, match="does not have"):
        catalog.materialise(
            dataset,
            tmp_path / "out",
            features=[FeatureSpec(name="s", source="label_set", ref="nope")],
        )
