"""Reporting while a dataset version is written.

Materialising was a hard link away and over before anyone looked. Pulling
tar shards out of a bucket is minutes, and minutes of no output is
indistinguishable from a hang — so the progress callback is part of the
interface rather than a nicety.
"""

import pytest

from strata.catalog import EVERYTHING
from strata.labels import Choices, ClassificationSchema


@pytest.fixture
def stocked(catalog, files):
    paths = files(5)
    ids = catalog.ingest(paths, media="image")
    label_set_id = catalog.label_sets.create("x", ClassificationSchema(classes=["a"]))
    catalog.annotate_many(label_set_id, [(i, Choices(values=["a"])) for i in ids])
    dataset_id = catalog.create_dataset("d", label_set_id, collections=EVERYTHING)
    return catalog, dataset_id


def test_progress_is_reported_for_every_sample(stocked, tmp_path):
    catalog, dataset_id = stocked
    seen = []
    catalog.materialise(dataset_id, tmp_path / "out", on_progress=lambda d, t: seen.append((d, t)))

    assert seen, "no progress at all — a caller cannot tell this from a hang"
    assert all(total == 5 for _, total in seen)
    assert [done for done, _ in seen] == sorted(done for done, _ in seen)


def test_progress_is_optional(stocked, tmp_path):
    catalog, dataset_id = stocked
    # The local pair needs no infrastructure and no caller ceremony
    assert catalog.materialise(dataset_id, tmp_path / "out").exists()
