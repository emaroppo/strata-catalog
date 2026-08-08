"""The schema and the queries against Postgres rather than SQLite.

Skipped unless a database is reachable, because the point of the local
backend is that a checkout runs with nothing installed. Where it does run it
covers the same ground as the SQLite suite — not for the sake of repetition,
but because "one schema, two dialects" is a claim that is either tested on
both or untested.

    docker compose up -d catalog-db
    STRATA_TEST_DB=postgresql+psycopg://strata:strata@localhost:5432/strata pytest
"""

import os
import uuid

import pytest

from strata.catalog import Catalog, LocalBackend
from strata.labels import Choices, ClassificationSchema

URL = os.environ.get(
    "STRATA_TEST_DB", "postgresql+psycopg://strata:strata@localhost:5432/strata"
)


def _reachable() -> bool:
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        return False
    try:
        with create_engine(URL).connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"no Postgres at {URL} (docker compose up -d catalog-db)"
)


@pytest.fixture
def catalog(tmp_path):
    """A catalog on Postgres, in a schema of its own.

    Per-test schemas rather than a shared one: these tests assert on counts,
    and a row left by another test is a failure that looks like a bug in the
    code under test. The schema has to exist before anything connects into
    it, since connecting runs create_all.
    """
    from sqlalchemy import create_engine, text

    name = f"t{uuid.uuid4().hex[:12]}"
    admin = create_engine(URL)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{name}"'))

    catalog = Catalog.connect(
        f"{URL}?options=-csearch_path%3D{name}", LocalBackend(tmp_path / "blobs")
    )
    yield catalog

    catalog.engine.dispose()
    with admin.begin() as conn:
        conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
    admin.dispose()


@pytest.fixture
def files(tmp_path):
    def _make(n: int = 10, prefix: str = "img"):
        root = tmp_path / "raw"
        root.mkdir(exist_ok=True)
        made = []
        for i in range(n):
            path = root / f"{prefix}{i:03d}.jpg"
            path.write_bytes(f"contents of {prefix}{i}".encode())
            made.append(path)
        return made

    return _make


def test_the_schema_creates(catalog):
    from sqlalchemy import inspect

    tables = set(inspect(catalog.engine).get_table_names())
    assert {"sample", "label_set", "annotation", "annotation_class",
            "dataset", "dataset_member"} <= tables


def test_ingest_and_dedup(catalog, files):
    paths = files(6)
    ids = catalog.ingest(paths, media="image")
    # The unique index on checksum has to behave the same on both dialects
    assert catalog.ingest(paths, media="image") == ids


def test_reserved_words_in_the_schema_are_quoted(catalog, files):
    # `offset` is reserved in SQL and `metadata` is an awkward name; if
    # either were unquoted this is where it would surface
    [sample_id] = catalog.ingest(files(1), media="image",
                                 metadata_for=lambda p: {"source_path": p.name})
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    [row] = catalog.unlabelled(label_set_id)
    assert row.location.offset == 0
    assert row.metadata["source_path"].endswith(".jpg")


def test_annotations_and_the_class_index(catalog, files):
    ids = catalog.ingest(files(4), media="image")
    label_set_id = catalog.create_label_set(
        "presence", ClassificationSchema(classes=["cat", "dog"])
    )
    catalog.annotate(ids[0], label_set_id, Choices(values=["cat"]))
    catalog.annotate(ids[1], label_set_id, Choices(values=["cat", "dog"]))

    assert {s.id for s in catalog.with_class(label_set_id, "cat")} == {ids[0], ids[1]}
    assert len(catalog.labelled(label_set_id)) == 2
    assert len(catalog.unlabelled(label_set_id)) == 2


def test_the_three_states_partition(catalog, files):
    ids = catalog.ingest(files(5), media="image")
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    catalog.annotate(ids[0], label_set_id, Choices(values=["a"]))
    catalog.skip(ids[1], label_set_id)
    assert (
        len(catalog.labelled(label_set_id)),
        len(catalog.skipped(label_set_id)),
        len(catalog.unlabelled(label_set_id)),
    ) == (1, 1, 3)


def test_a_grouped_split_holds(catalog, files, tmp_path):
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    for group in range(4):
        ids = catalog.ingest(
            files(5, prefix=f"v{group}_"), media="image",
            subtype="frames", group_id=f"vid{group}",
        )
        catalog.annotate_many(label_set_id, [(i, Choices(values=["a"])) for i in ids])

    dataset_id = catalog.create_dataset("d", label_set_id)
    from strata.catalog import Manifest

    directory = catalog.materialise(dataset_id, tmp_path / "out")
    manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())

    sides = {}
    for sample in manifest.samples:
        sides.setdefault(sample.group_id, set()).add(sample.val)
    assert all(len(v) == 1 for v in sides.values())


def test_a_version_is_reused_when_the_selection_has_not_changed(catalog, files):
    ids = catalog.ingest(files(6), media="image")
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    catalog.annotate_many(label_set_id, [(i, Choices(values=["a"])) for i in ids])
    assert catalog.create_dataset("d", label_set_id) == catalog.create_dataset("d", label_set_id)


def test_by_location_resolves(catalog, files):
    catalog.ingest(files(3), media="image")
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    row = catalog.unlabelled(label_set_id)[0]
    assert catalog.by_location(row.location.container).id == row.id
