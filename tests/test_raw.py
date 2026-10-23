"""Raw files: kept as they arrived, under the name they arrived by.

What matters is that a name never quietly changes its bytes, that the same
bytes are stored once however many names they have, and that an ingest
refused for one file writes nothing for any. See docs/adr/0045.
"""

import hashlib

import pytest
from sqlalchemy import func, select

from strata.catalog import Catalog, CatalogError, Location, copy_index, repack_blobs
from strata.catalog.index import tables as t
from strata.catalog.index.runs import files_digest
from strata.catalog.storage.s3 import S3Backend


@pytest.fixture
def catalog(tmp_path):
    return Catalog.local(tmp_path / "catalog")


@pytest.fixture
def maildir(tmp_path):
    """Enron's shape: no suffixes, and one message filed under two folders."""
    root = tmp_path / "maildir"
    for path, body in {
        "quenet-j/inbox/6.": b"a message",
        "quenet-j/all_documents/1.": b"a message",
        "quenet-j/sent/2.": b"another one",
        "lay-k/inbox/1.": b"a third",
    }.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(body)
    return root


def _count(catalog, table) -> int:
    with catalog.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_every_file_under_the_root_is_named_by_its_path(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    assert [row.name for row in catalog.raw.names("enron/maildir")] == [
        "enron/maildir/lay-k/inbox/1.",
        "enron/maildir/quenet-j/all_documents/1.",
        "enron/maildir/quenet-j/inbox/6.",
        "enron/maildir/quenet-j/sent/2.",
    ]


def test_the_same_bytes_under_two_names_are_two_rows_over_one_blob(catalog, maildir):
    report = catalog.ingest_raw(maildir, "enron/maildir")
    assert (report.files, report.checksums, report.blobs) == (4, 3, 3)
    rows = {row.path: row for row in catalog.raw.names("enron/maildir")}
    assert rows["quenet-j/inbox/6."].location == rows["quenet-j/all_documents/1."].location


def test_a_blob_reads_back_as_the_file(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    for row in catalog.raw.names("enron/maildir"):
        assert catalog.blobs.get(row.location) == (maildir / row.path).read_bytes()


def test_ingesting_again_writes_nothing_but_the_run(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    again = catalog.ingest_raw(maildir, "enron/maildir")
    assert (again.added, again.held, again.blobs) == (0, 4, 0)
    assert _count(catalog, t.raw) == 4
    # The attempt shows in the history
    assert [run.counts["added"] for run in catalog.runs.every()] == [4, 0]


def test_a_name_held_with_other_bytes_refuses_the_whole_ingest(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    (maildir / "quenet-j/sent/2.").write_bytes(b"edited")
    (maildir / "quenet-j/sent/3.").write_bytes(b"new")
    with pytest.raises(CatalogError, match=r"enron/maildir/quenet-j/sent/2\."):
        catalog.ingest_raw(maildir, "enron/maildir")
    # Not even the new file, and no run
    assert _count(catalog, t.raw) == 4
    assert len(catalog.runs.every()) == 1


def test_a_changed_source_goes_into_a_collection_of_its_own(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    (maildir / "quenet-j/sent/2.").write_bytes(b"edited")
    catalog.ingest_raw(maildir, "enron/maildir-v2")
    assert _count(catalog, t.raw) == 8


def test_a_list_says_which_files_go_in(catalog, maildir):
    report = catalog.ingest_raw(maildir, "enron/maildir", files=["quenet-j/inbox/6.", ""])
    assert report.files == 1
    assert [row.path for row in catalog.raw.names("enron/maildir")] == ["quenet-j/inbox/6."]


def test_an_absolute_listed_path_inside_the_root_is_named_from_the_root(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir", files=[str(maildir / "lay-k/inbox/1.")])
    assert [row.path for row in catalog.raw.names("enron/maildir")] == ["lay-k/inbox/1."]


@pytest.mark.parametrize("entry", ["../elsewhere", "/etc/hostname"])
def test_a_listed_path_outside_the_root_is_refused(catalog, maildir, entry):
    with pytest.raises(CatalogError, match="not under"):
        catalog.ingest_raw(maildir, "enron/maildir", files=[entry])


def test_a_listed_file_that_is_not_there_refuses_the_ingest(catalog, maildir):
    with pytest.raises(CatalogError, match=r"lay-k/inbox/9\."):
        catalog.ingest_raw(maildir, "enron/maildir", files=["lay-k/inbox/1.", "lay-k/inbox/9."])
    assert _count(catalog, t.raw) == 0


@pytest.mark.parametrize("collection", ["", "/enron", "enron/", "enron//maildir"])
def test_a_collection_has_no_empty_segment(catalog, maildir, collection):
    with pytest.raises(CatalogError, match="not a collection"):
        catalog.ingest_raw(maildir, collection)


def test_a_run_records_the_checksums_it_was_given(catalog, maildir):
    report = catalog.ingest_raw(maildir, "enron/maildir")
    run = catalog.runs.get(report.run_id)
    checksums = {row.checksum for row in catalog.raw.names("enron/maildir")}
    with catalog.engine.connect() as conn:
        assert catalog.runs.inputs(conn, run.id) == sorted(checksums)
    assert run.files_digest == files_digest(checksums)
    assert (run.kind, run.spec) == ("ingest", {"collection": "enron/maildir"})
    assert run.finished_at is not None


def test_a_sample_over_a_raw_files_bytes_shares_its_blob(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    [sample_id] = catalog.ingest([maildir / "lay-k/inbox/1."], media="text")
    raw = {row.path: row for row in catalog.raw.names("enron/maildir")}["lay-k/inbox/1."]
    with catalog.engine.connect() as conn:
        location = conn.execute(
            select(t.sample.c.location).where(t.sample.c.id == sample_id)
        ).scalar_one()
    assert location == raw.location.container


# ----------------------------------------------------------------------
# Resolving the names a run is handed
# ----------------------------------------------------------------------


def test_names_resolve_to_checksums_and_the_unknown_are_listed(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    with catalog.engine.connect() as conn:
        found, missing = catalog.raw.resolve(
            conn, ["enron/maildir/quenet-j/inbox/6.", "enron/maildir/nope", "elsewhere/x"]
        )
    assert found == {"enron/maildir/quenet-j/inbox/6.": hashlib.sha256(b"a message").hexdigest()}
    assert missing == ["enron/maildir/nope", "elsewhere/x"]


def test_a_name_belongs_to_the_longest_collection_it_starts_with(catalog, maildir, tmp_path):
    other = tmp_path / "other"
    (other / "maildir").mkdir(parents=True)
    (other / "maildir/x").write_bytes(b"from enron")
    catalog.ingest_raw(other, "enron")
    catalog.ingest_raw(maildir, "enron/maildir")
    with catalog.engine.connect() as conn:
        found, _ = catalog.raw.resolve(conn, ["enron/maildir/x", "enron/maildir/lay-k/inbox/1."])
    # enron/maildir/x is the path maildir/x in enron, since nothing named
    # x was ingested into enron/maildir
    assert set(found) == {"enron/maildir/lay-k/inbox/1."}


def test_a_checksum_stands_for_itself(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    checksum = hashlib.sha256(b"a third").hexdigest()
    with catalog.engine.connect() as conn:
        found, missing = catalog.raw.resolve(conn, [checksum, "0" * 64])
    assert found == {checksum: checksum}
    assert missing == ["0" * 64]


# ----------------------------------------------------------------------
# Moving with the rest of the catalog
# ----------------------------------------------------------------------


class _Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key, Range=""):
        body = self.objects[Key]
        if Range:
            first, last = Range.removeprefix("bytes=").split("-")
            body = body[int(first) : int(last) + 1]
        return {"Body": type("R", (), {"read": lambda self: body})()}


def test_a_repack_moves_raw_rows_and_packs_each_checksum_once(catalog, maildir):
    catalog.ingest_raw(maildir, "enron/maildir")
    catalog.ingest([maildir / "lay-k/inbox/1."], media="text")
    target = S3Backend(_Store(), bucket="b")

    report = repack_blobs(catalog, target)

    assert (report.samples, report.raw) == (1, 4)
    assert report.bytes == len(b"a message") + len(b"another one") + len(b"a third")
    for row in catalog.raw.names("enron/maildir"):
        assert row.location.container.startswith("shards/")
        assert target.get(row.location) == (maildir / row.path).read_bytes()
    with catalog.engine.connect() as conn:
        sample = conn.execute(select(t.sample.c.location, t.sample.c.offset)).one()
    raw = {row.path: row for row in catalog.raw.names("enron/maildir")}["lay-k/inbox/1."]
    assert Location(sample.location, sample.offset, raw.location.length) == raw.location
    assert repack_blobs(catalog, target).raw == 0


def test_a_copy_carries_runs_and_raw_rows(catalog, maildir, tmp_path):
    report = catalog.ingest_raw(maildir, "enron/maildir")
    copy = Catalog.create(f"sqlite:///{tmp_path / 'copy.db'}", catalog.blobs)
    copy_index(catalog, copy)
    assert [row.name for row in copy.raw.names("enron/maildir")] == [
        row.name for row in catalog.raw.names("enron/maildir")
    ]
    assert copy.runs.get(report.run_id).counts == catalog.runs.get(report.run_id).counts
