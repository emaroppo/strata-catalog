"""A dataset version on disk: when one already there is reused, and when not.

The rule lived in two places — the laptop's round and the modelling host's —
and drifted, so remote rounds trained without features. It lives in one
place now, and these are its cases, against a real catalog.
"""

import json

import pytest

from strata.catalog import EVERYTHING, ensure_materialised
from strata.catalog.features import FeatureSpec
from strata.labels import MANIFEST_NAME, Choices


@pytest.fixture
def version(catalog, files, label_set) -> int:
    ids = catalog.ingest(files(4), media="image")
    catalog.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids])
    return catalog.create_dataset("d", label_set, collections=EVERYTHING)


@pytest.fixture
def fetches(catalog, monkeypatch) -> list:
    """Every call to materialise from here on, which is what a rebuild costs."""
    calls = []
    real = catalog.materialise

    def counted(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("dataset_id"))
        return real(*args, **kwargs)

    monkeypatch.setattr(catalog, "materialise", counted)
    return calls


def _rewrite(directory, change) -> None:
    path = directory / MANIFEST_NAME
    written = json.loads(path.read_text())
    change(written)
    path.write_text(json.dumps(written))


def test_a_version_lives_under_its_name_and_number(catalog, version, tmp_path):
    result = ensure_materialised(catalog, version, tmp_path)
    assert result.directory == tmp_path / "d" / "v001"
    assert result.manifest.catalog_id == catalog.id
    assert (result.directory / MANIFEST_NAME).exists()


def test_a_retry_reuses_what_is_already_there(catalog, version, tmp_path, fetches):
    """A round that ran out of memory is retried; the version it built stands."""
    catalog.materialise(version, tmp_path / "d" / "v001")
    fetches.clear()

    again = ensure_materialised(catalog, version, tmp_path)

    assert fetches == []
    assert again.fetched == 0


def test_a_change_of_features_is_rebuilt(catalog, version, tmp_path, fetches):
    ensure_materialised(catalog, version, tmp_path)
    fetches.clear()
    where = FeatureSpec(name="where", source="metadata", ref="source_path")

    result = ensure_materialised(catalog, version, tmp_path, features=[where])

    assert fetches == [version]
    assert result.manifest.features == [where.as_dict()]


def test_another_catalogs_version_of_the_same_name_is_rebuilt(
    catalog, version, tmp_path, fetches
):
    """After a switch to a rebuilt catalog, whose numbering starts again."""
    first = ensure_materialised(catalog, version, tmp_path)
    _rewrite(first.directory, lambda m: m.update(catalog_id="20250101T000000-deadbeef"))
    fetches.clear()

    result = ensure_materialised(catalog, version, tmp_path)

    assert fetches == [version]
    assert result.manifest.catalog_id == catalog.id


def test_a_manifest_this_release_cannot_read_is_rebuilt(catalog, version, tmp_path, fetches):
    first = ensure_materialised(catalog, version, tmp_path)
    _rewrite(first.directory, lambda m: m.pop("format"))
    fetches.clear()

    ensure_materialised(catalog, version, tmp_path)

    assert fetches == [version]


def test_an_interrupted_fetch_is_not_kept(catalog, version, tmp_path):
    pending = tmp_path / "d" / "pending"
    pending.mkdir(parents=True)
    (pending / "half-written").write_bytes(b"part of a sample")

    result = ensure_materialised(catalog, version, tmp_path)

    assert not pending.exists()
    assert not (result.directory / "half-written").exists()
