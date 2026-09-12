"""Every label type through the catalog.

The catalog stores annotations it does not otherwise understand, so each
type is checked here against the examples ``strata.labels`` ships: a type
added there is covered here on the next upgrade, and fails until the
catalog handles it. Six places once assumed classification, none of them
raised, and each was found by hand.
"""

import pytest

from strata.labels import MANIFEST_NAME, Manifest, SchemaError, Span, Spans, SpanSchema
from strata.labels.examples import EXAMPLES

each_type = pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e.name)


def _samples(catalog, tmp_path, media: str) -> list[int]:
    """Two, because a group is indivisible and one cannot be split.

    Ingested as the media the type is for: a type exercised as the wrong one
    still passes everything here, which is how spans went through as images.
    """
    suffix = {"text": "txt"}.get(media, "bin")
    sources = []
    for i in range(2):
        source = tmp_path / f"sample{i}.{suffix}"
        source.write_bytes(f"sample {i}".encode())
        sources.append(source)
    return catalog.ingest(sources, media=media)


@each_type
def test_a_label_set_keeps_its_schema(catalog, example):
    catalog.label_sets.create("x", example.schema)
    assert catalog.label_sets.get("x")[1] == example.schema


@each_type
def test_an_annotation_survives_the_catalog(catalog, tmp_path, example):
    [sample_id, _] = _samples(catalog, tmp_path, example.media)
    label_set_id = catalog.label_sets.create("x", example.schema)
    catalog.annotations.annotate(sample_id, label_set_id, example.value)
    # The whole point of the catalog: what a person said outlives the tool
    # that collected it, unchanged
    assert catalog.annotations.annotation_of(sample_id, label_set_id) == example.value


@each_type
def test_an_empty_answer_is_not_the_same_as_no_answer(catalog, tmp_path, example):
    [sample_id, _] = _samples(catalog, tmp_path, example.media)
    label_set_id = catalog.label_sets.create("x", example.schema)
    empty = type(example.value)()
    catalog.annotations.annotate(sample_id, label_set_id, empty)
    # A reviewer who looked and found none of the classes present has
    # answered. Only the annotation existing distinguishes that from a
    # sample nobody has seen.
    assert catalog.annotations.annotation_of(sample_id, label_set_id) == empty
    assert catalog.samples.labelled(label_set_id, "*")


@each_type
def test_a_materialised_dataset_carries_the_type(catalog, tmp_path, example):
    label_set_id = catalog.label_sets.create("x", example.schema)
    for sample_id in _samples(catalog, tmp_path, example.media):
        catalog.annotations.annotate(sample_id, label_set_id, example.value)
    dataset_id = catalog.create_dataset("d", label_set_id, collections="*")

    directory = catalog.materialise(dataset_id, tmp_path / "out")
    manifest = Manifest.model_validate_json((directory / MANIFEST_NAME).read_text())
    assert manifest.label_schema == example.schema
    assert manifest.samples[0].value == example.value


def test_an_undeclared_overlap_is_refused_where_it_would_be_stored(catalog, tmp_path):
    """The refusal has to fire on the path a reviewer's answer takes.

    Label Studio will let anyone draw two regions across one phrase. Until
    the label set says whether that is meaningful here, storing it means a
    tagger silently training on whichever of the two came last.
    """
    document = tmp_path / "doc.txt"
    document.write_text("Ada Lovelace worked here")
    [sample_id] = catalog.ingest([document], media="text")
    label_set_id = catalog.label_sets.create("x", SpanSchema(classes=["name", "place"]))

    with pytest.raises(SchemaError, match="overlap"):
        catalog.annotations.annotate(
            sample_id,
            label_set_id,
            Spans(
                values=[
                    Span(labels=["name"], start=0, end=12),
                    Span(labels=["place"], start=4, end=20),
                ]
            ),
        )
