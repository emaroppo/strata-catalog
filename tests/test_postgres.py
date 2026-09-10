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

from strata.catalog import EVERYTHING, Catalog, LocalBackend
from strata.labels import Choices, ClassificationSchema


def _default_url() -> str:
    """The compose database, described the way compose describes it.

    Built from the same variables docker-compose reads rather than hardcoded,
    so rotating the password in ``.env`` does not quietly switch this whole
    file off — which is exactly what a literal default did once.
    """
    user = os.environ.get("STRATA_DB_USER", "strata")
    password = os.environ.get("STRATA_DB_PASSWORD", "strata")
    port = os.environ.get("STRATA_DB_PORT", "5432")
    name = os.environ.get("STRATA_DB_NAME", "strata")
    return f"postgresql+psycopg://{user}:{password}@localhost:{port}/{name}"


URL = os.environ.get("STRATA_TEST_DB") or _default_url()


def _why_not() -> str | None:
    """None if the database is usable, else why it is not.

    The reason is reported verbatim. A skip that says "no Postgres" when the
    container is running and the password is wrong sends you to look in the
    wrong place, and a whole dialect goes untested while you do.
    """
    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        return "psycopg not installed (uv sync --extra postgres)"
    try:
        with create_engine(URL).connect() as conn:
            conn.execute(text("SELECT 1"))
        return None
    except Exception as e:
        return f"{type(e).__name__}: {str(e).splitlines()[0]}"


_UNUSABLE = _why_not()

pytestmark = pytest.mark.skipif(
    _UNUSABLE is not None,
    reason=(
        f"Postgres unusable — {_UNUSABLE}. Set $STRATA_TEST_DB, or load the "
        f"compose environment (set -a; . ./.env; set +a)."
    ),
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
    [row] = catalog.unlabelled(label_set_id, EVERYTHING)
    assert row.location.offset == 0
    assert row.metadata["source_path"].endswith(".jpg")


def test_annotations_and_the_class_index(catalog, files):
    ids = catalog.ingest(files(4), media="image")
    label_set_id = catalog.create_label_set(
        "presence", ClassificationSchema(classes=["cat", "dog"])
    )
    catalog.annotate(ids[0], label_set_id, Choices(values=["cat"]))
    catalog.annotate(ids[1], label_set_id, Choices(values=["cat", "dog"]))

    assert {s.id for s in catalog.with_class(label_set_id, "cat", EVERYTHING)} == {ids[0], ids[1]}
    assert len(catalog.labelled(label_set_id, EVERYTHING)) == 2
    assert len(catalog.unlabelled(label_set_id, EVERYTHING)) == 2


def test_the_three_states_partition(catalog, files):
    ids = catalog.ingest(files(5), media="image")
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    catalog.annotate(ids[0], label_set_id, Choices(values=["a"]))
    catalog.skip(ids[1], label_set_id)
    assert (
        len(catalog.labelled(label_set_id, EVERYTHING)),
        len(catalog.skipped(label_set_id, EVERYTHING)),
        len(catalog.unlabelled(label_set_id, EVERYTHING)),
    ) == (1, 1, 3)


def test_a_grouped_split_holds(catalog, files, tmp_path):
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    for group in range(4):
        ids = catalog.ingest(
            files(5, prefix=f"v{group}_"), media="image",
            subtype="frames", group_id=f"vid{group}",
        )
        catalog.annotate_many(label_set_id, [(i, Choices(values=["a"])) for i in ids])

    dataset_id = catalog.create_dataset("d", label_set_id, collections=EVERYTHING)
    from strata.labels import Manifest

    directory = catalog.materialise(dataset_id, tmp_path / "out")
    manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())

    sides = {}
    for sample in manifest.samples:
        sides.setdefault(sample.group_id, set()).add(sample.split)
    assert all(len(v) == 1 for v in sides.values())


def test_a_version_is_reused_when_the_selection_has_not_changed(catalog, files):
    ids = catalog.ingest(files(6), media="image")
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    catalog.annotate_many(label_set_id, [(i, Choices(values=["a"])) for i in ids])
    first = catalog.create_dataset("d", label_set_id, collections=EVERYTHING)
    assert first == catalog.create_dataset("d", label_set_id, collections=EVERYTHING)


def test_by_location_resolves(catalog, files):
    catalog.ingest(files(3), media="image")
    label_set_id = catalog.create_label_set("x", ClassificationSchema(classes=["a"]))
    row = catalog.unlabelled(label_set_id, EVERYTHING)[0]
    assert catalog.by_location(row.location.container).id == row.id


# ----------------------------------------------------------------------
# Moving an index
# ----------------------------------------------------------------------


@pytest.fixture
def populated(tmp_path, files):
    """A SQLite catalog with something of every kind in it."""
    source = Catalog.local(tmp_path / "sqlite")
    label_set_id = source.create_label_set(
        "presence", ClassificationSchema(classes=["cat", "dog"])
    )
    # Two groups, because one cannot be split and create_dataset says so
    ids = []
    for group in ("vid1", "vid2"):
        ids += source.ingest(
            files(4, prefix=f"{group}_"), media="image", subtype="frames",
            group_id=group, metadata_for=lambda p: {"source_path": p.name},
        )
    source.annotate_many(
        label_set_id, [(i, Choices(values=["cat"])) for i in ids[:5]]
    )
    source.skip(ids[5], label_set_id)
    source.create_dataset("d", label_set_id, collections=EVERYTHING)
    return source, label_set_id, ids


def test_every_row_crosses(populated, catalog):
    from strata.catalog import copy_index

    source, _, _ = populated
    report = copy_index(source, catalog)
    assert report.copied["sample"] == 8
    assert report.copied["annotation"] == 6  # five answered, one skipped
    assert report.copied["dataset_member"] == 5
    assert report.total > 0


def test_sample_ids_are_preserved(populated, catalog):
    from strata.catalog import copy_index

    source, label_set_id, ids = populated
    copy_index(source, catalog)
    # Annotations, dataset members and the Label Studio task map are all
    # keyed on these; renumbering would repoint every task at another image
    assert {s.id for s in catalog.labelled(label_set_id, EVERYTHING)} == set(ids[:5])


def test_annotations_and_their_index_arrive(populated, catalog):
    from strata.catalog import copy_index

    source, label_set_id, ids = populated
    copy_index(source, catalog)
    assert catalog.annotation_of(ids[0], label_set_id) == Choices(values=["cat"])
    assert len(catalog.with_class(label_set_id, "cat", EVERYTHING)) == 5
    assert len(catalog.skipped(label_set_id, EVERYTHING)) == 1


def test_grouping_and_metadata_survive(populated, catalog):
    from strata.catalog import copy_index

    source, label_set_id, _ = populated
    copy_index(source, catalog)
    rows = catalog.labelled(label_set_id, EVERYTHING)
    assert {r.group_id for r in rows} == {"vid1", "vid2"}
    assert all((r.metadata or {}).get("source_path") for r in rows)


def test_the_next_insert_does_not_collide(populated, catalog, tmp_path):
    from strata.catalog import copy_index

    source, label_set_id, _ = populated
    copy_index(source, catalog)

    # Ids came in explicitly, which leaves a Postgres sequence at zero — the
    # next insert would try row 1 and hit something already there
    extra = tmp_path / "raw" / "later.jpg"
    extra.write_bytes(b"ingested after the copy")
    [new_id] = catalog.ingest([extra], media="image")
    assert new_id > 8


def test_copying_into_a_populated_index_is_refused(populated, catalog, files):
    from strata.catalog import CopyError, copy_index

    source, _, _ = populated
    catalog.ingest(files(1, prefix="already"), media="image")
    # Merging two catalogs is a different problem; doing it by accident here
    # would be worse than not offering it
    with pytest.raises(CopyError, match="already holds"):
        copy_index(source, catalog)


def test_a_dataset_still_materialises_after_the_move(populated, catalog, tmp_path):
    from strata.catalog import copy_index
    from strata.labels import Manifest

    source, label_set_id, _ = populated
    copy_index(source, catalog)

    # Pointed at the source's blobs, which is the whole arrangement: the
    # index moved and the bytes did not. A target aimed anywhere else has an
    # index describing files that are not there.
    catalog.blobs = source.blobs
    dataset_id = catalog.create_dataset("d2", label_set_id, collections=EVERYTHING)
    directory = catalog.materialise(dataset_id, tmp_path / "out")
    manifest = Manifest.model_validate_json((directory / "manifest.json").read_text())
    # The blobs never moved, so the files behind the manifest are the ones
    # the source catalog wrote
    assert len(manifest.samples) == 5
    assert all((directory / s.path).exists() for s in manifest.samples)
