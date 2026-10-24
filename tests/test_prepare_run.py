"""Preparing samples from raw files: what a run writes, skips, refuses and counts.

The stages here are stand-ins registered through the engine's own seam;
what is under test is the catalog's half: inputs resolved and handed over
once, outputs stored once by checksum, an origin row per input, metadata
combined from origins, and the run's counts adding up. See docs/adr/0045,
docs/adr/0047 and docs/adr/0049.
"""

import json
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from sqlalchemy import func, select

from strata.catalog import Catalog, CatalogError
from strata.catalog.cli import main
from strata.catalog.index import tables as t
from strata.catalog.prepare_run import load_chain, prepare
from strata.catalog.storage.s3 import S3Backend
from strata.catalog.verify import verify_run
from strata.contracts import Choices, ClassificationSchema, Text
from strata.contracts import sample_types as types
from strata.prepare.chain import Drop, Item, Split, StageParams, Transform


class Note(Text):
    segment: ClassVar[str] = "note"

    def facts(self, data: bytes) -> dict[str, Any]:
        return {"characters": len(data.decode())}


class Headers(Transform):
    """The first line is a folder header, the rest the body: Enron's two copies."""

    name = "headers"
    version = "1"
    produces = "note"

    def transform(self, item, source):
        folder, _, body = item.body.partition(b"\n")
        return Item(body, {"folder": folder.decode(), "names": list(source.names)})


class EmptyParams(StageParams):
    marker: str = "EMPTY"


class Empty(Transform[EmptyParams]):
    """Hands on nothing for a body that is only the marker, via a split."""

    name = "empty"
    version = "1"
    accepts = "note"
    produces = "note"
    Params = EmptyParams

    def transform(self, item, source):
        return item


class Paragraphs(Split[EmptyParams]):
    name = "paragraphs"
    version = "1"
    accepts = "note"
    produces = "note"
    Params = EmptyParams

    def split(self, item, source):
        for part in item.body.split(b"\n\n"):
            yield (
                Drop("empty_body")
                if part.strip() == self.params.marker.encode()
                else Item(part, item.facts)
            )


class Broken(Transform):
    name = "broken"
    version = "1"
    accepts = "note"
    produces = "note"

    def transform(self, item, source):
        raise RuntimeError("a fault in the code")


def _entry(name, obj):
    return SimpleNamespace(
        name=name,
        value=f"{obj.__module__}:{obj.__name__}",
        dist=SimpleNamespace(name="test-stages", version="9.9"),
        load=lambda: obj,
    )


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    stages = [Headers, Empty, Paragraphs, Broken]
    monkeypatch.setattr("strata.prepare.chain.entries", lambda: [_entry(s.name, s) for s in stages])
    builtin = types.entries()
    monkeypatch.setattr(types, "entries", lambda: [*builtin, _entry("note", Note)])


def _spec(tmp_path, *stages: str, marker: str | None = None, name: str = "spec.toml"):
    blocks = []
    for stage in stages:
        blocks.append(f'[[stage]]\nname = "{stage}"')
        if marker is not None and stage == "paragraphs":
            blocks.append(f'marker = "{marker}"')
    path = tmp_path / name
    path.write_text(
        'name = "notes"\n\n'
        + "\n".join(blocks)
        + '\n\n[output]\ncollection = "notes/single"\ntype = "note"\n'
    )
    return path


@pytest.fixture
def maildir(tmp_path):
    root = tmp_path / "maildir"
    for path, body in {
        # The same message under two folders: different bytes, one body
        "j/inbox/6.": b"inbox\nhello there",
        "j/all_documents/1.": b"all_documents\nhello there",
        "j/sent/2.": b"sent\nfirst\n\nsecond",
        "k/inbox/1.": b"inbox\nEMPTY",
    }.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(body)
    return root


@pytest.fixture
def catalog(tmp_path, maildir):
    catalog = Catalog.local(tmp_path / "catalog")
    catalog.ingest_raw(maildir, "enron/maildir")
    return catalog


def _all(catalog) -> list[str]:
    return [row.name for row in catalog.raw.names("enron/maildir")]


def _rows(catalog, table) -> int:
    with catalog.engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _metadata(catalog, body: str) -> dict:
    with catalog.engine.connect() as conn:
        rows = conn.execute(select(t.sample.c.id, t.sample.c.metadata)).all()
        for sample_id, metadata in rows:
            [(stored,)] = conn.execute(
                select(t.sample.c.location).where(t.sample.c.id == sample_id)
            ).all()
            if catalog.blobs.root.joinpath(stored).read_bytes() == body.encode():
                return metadata
    raise AssertionError(f"no sample {body!r}")


# ----------------------------------------------------------------------


def test_a_run_writes_a_sample_per_body_and_an_origin_per_input(catalog, tmp_path):
    chain = load_chain(_spec(tmp_path, "headers", "paragraphs"))
    report = prepare(catalog, chain, _all(catalog))

    c = report.counts
    assert (c["inputs"], c["produced"], c["emptied"]) == (4, 3, {"paragraphs/empty_body": 1})
    # hello there (from two inputs), first, second
    assert (c["samples"], c["new_samples"]) == (3, 3)
    assert _rows(catalog, t.origin) == 4
    assert verify_run(catalog, report.run_id, deep=True).ok


def test_an_output_whose_bytes_another_type_holds_is_a_sample_of_its_own(catalog, tmp_path):
    # The same bytes as two types are two samples over one blob. docs/adr/0052
    (tmp_path / "plain.txt").write_bytes(b"hello there")
    [plain] = catalog.ingest(
        [tmp_path / "plain.txt"], media="text", metadata={"kept": True}, collections=["loose"]
    )
    report = prepare(catalog, load_chain(_spec(tmp_path, "headers")), _all(catalog))

    assert report.counts["new_samples"] == report.counts["samples"]
    [loose] = catalog.samples.matching(["loose"])
    note = catalog.samples.by_checksum(loose.checksum, "text/note")
    assert (loose.id, loose.type, loose.metadata) == (plain, "text/plain", {"kept": True})
    assert note is not None and note.id != plain and note.location == loose.location


def test_samples_land_in_the_specs_collection_as_its_type(catalog, tmp_path):
    prepare(catalog, load_chain(_spec(tmp_path, "headers", "paragraphs")), _all(catalog))
    with catalog.engine.connect() as conn:
        assert set(conn.execute(select(t.sample_collection.c.collection)).scalars()) == {
            "notes/single"
        }
        assert set(conn.execute(select(t.sample.c.subtype)).scalars()) == {"note"}


def test_a_samples_metadata_is_its_bytes_facts_then_its_first_origins(catalog, tmp_path):
    prepare(catalog, load_chain(_spec(tmp_path, "headers")), _all(catalog))
    metadata = _metadata(catalog, "hello there")
    assert metadata["characters"] == len("hello there")
    with catalog.engine.connect() as conn:
        recorded = list(conn.execute(select(t.origin.c.metadata).order_by(t.origin.c.id)).scalars())
    first = next(row["folder"] for row in recorded if row["folder"] in ("inbox", "all_documents"))
    # Inputs run in checksum order; whichever copy was recorded first wins
    assert metadata["folder"] == first
    folders = sorted(row["folder"] for row in recorded)
    # Each origin keeps its own occurrence
    assert folders == ["all_documents", "inbox", "inbox", "sent"]


def test_a_stage_is_handed_every_name_its_input_has(catalog, tmp_path, maildir):
    (maildir / "j/copy").mkdir()
    (maildir / "j/copy/6.").write_bytes((maildir / "j/inbox/6.").read_bytes())
    catalog.ingest_raw(maildir, "enron/maildir")
    prepare(catalog, load_chain(_spec(tmp_path, "headers")), ["enron/maildir/j/copy/6."])
    with catalog.engine.connect() as conn:
        [names] = [row["names"] for row in conn.execute(select(t.origin.c.metadata)).scalars()]
    assert names == ["enron/maildir/j/copy/6.", "enron/maildir/j/inbox/6."]


def test_a_name_nothing_ingested_refuses_the_run(catalog, tmp_path):
    chain = load_chain(_spec(tmp_path, "headers"))
    with pytest.raises(CatalogError, match=r"enron/maildir/j/inbox/404\."):
        prepare(catalog, chain, [*_all(catalog), "enron/maildir/j/inbox/404."])
    assert _rows(catalog, t.sample) == 0
    assert catalog.runs.every("prepare") == []


def test_missing_names_can_be_counted_instead(catalog, tmp_path):
    chain = load_chain(_spec(tmp_path, "headers"))
    report = prepare(catalog, chain, [*_all(catalog), "x/y"], allow_missing=True)
    assert (report.counts["missing"], report.counts["names"]) == (1, 5)


def test_the_same_chain_again_writes_nothing_new_and_still_records_the_run(catalog, tmp_path):
    chain = load_chain(_spec(tmp_path, "headers", "paragraphs"))
    prepare(catalog, chain, _all(catalog)[:2])
    again = prepare(catalog, chain, _all(catalog))
    # The two copies of hello there were prepared; first and second come
    # from one more input, and the empty one is counted
    assert (again.counts["already"], again.counts["produced"]) == (2, 1)
    assert again.counts["emptied"] == {"paragraphs/empty_body": 1}
    assert again.counts["inputs"] == 4
    assert verify_run(catalog, again.run_id).ok
    assert len(catalog.runs.every("prepare")) == 2


def test_a_different_chain_over_prepared_inputs_must_say_what_it_continues(catalog, tmp_path):
    first = prepare(catalog, load_chain(_spec(tmp_path, "headers", "paragraphs")), _all(catalog))
    changed = load_chain(_spec(tmp_path, "headers", "paragraphs", marker="NONE", name="b.toml"))
    with pytest.raises(CatalogError, match=first.run_id):
        prepare(catalog, changed, _all(catalog))

    report = prepare(catalog, changed, _all(catalog), continues=first.run_id)
    assert report.counts["superseded"] == 3
    assert catalog.runs.get(report.run_id).continues == first.run_id


def test_supersession_counts_what_a_person_answered(catalog, tmp_path):
    first = prepare(catalog, load_chain(_spec(tmp_path, "headers", "paragraphs")), _all(catalog))
    label_set = catalog.label_sets.create("ok", ClassificationSchema(classes=["yes"]))
    with catalog.engine.connect() as conn:
        some = conn.execute(select(t.sample.c.id).limit(1)).scalar_one()
    catalog.annotations.annotate(some, label_set, Choices(values=["yes"]))
    changed = load_chain(_spec(tmp_path, "headers", "paragraphs", marker="NONE", name="b.toml"))
    report = prepare(catalog, changed, _all(catalog), continues=first.run_id)
    assert report.counts["superseded_answered"] == 1


def test_a_run_continues_the_line_behind_the_run_it_names(catalog, tmp_path):
    a = prepare(catalog, load_chain(_spec(tmp_path, "headers", "paragraphs")), _all(catalog))
    b_chain = load_chain(_spec(tmp_path, "headers", "paragraphs", marker="B", name="b.toml"))
    b = prepare(catalog, b_chain, _all(catalog), continues=a.run_id)
    c_chain = load_chain(_spec(tmp_path, "headers", "paragraphs", marker="C", name="c.toml"))
    # Continuing a skips b, which also prepared these inputs
    with pytest.raises(CatalogError, match=b.run_id):
        prepare(catalog, c_chain, _all(catalog), continues=a.run_id)
    prepare(catalog, c_chain, _all(catalog), continues=b.run_id)


def test_a_stage_that_raises_leaves_nothing(catalog, tmp_path):
    with pytest.raises(CatalogError, match=r"Stage 'broken' failed on input [0-9a-f]{64}"):
        prepare(catalog, load_chain(_spec(tmp_path, "headers", "broken")), _all(catalog))
    assert _rows(catalog, t.sample) == 0
    assert _rows(catalog, t.run) == 1  # the ingest's


def test_a_spec_the_engine_refuses_is_a_catalog_error(catalog, tmp_path):
    with pytest.raises(CatalogError, match="nowhere"):
        load_chain(_spec(tmp_path, "nowhere"))


def test_inputs_come_from_object_storage_when_nothing_is_local(tmp_path, maildir, object_store):
    store = S3Backend(object_store, bucket="b")
    catalog = Catalog.create(f"sqlite:///{tmp_path / 'c.db'}", store)
    catalog.ingest_raw(maildir, "enron/maildir")
    report = prepare(catalog, load_chain(_spec(tmp_path, "headers", "paragraphs")), _all(catalog))
    assert report.counts["samples"] == 3
    assert verify_run(catalog, report.run_id, deep=True).ok


def test_the_command_reads_its_list_from_stdin(catalog, tmp_path, monkeypatch, capsys):
    import io

    config = tmp_path / "config.toml"
    config.write_text(f'[catalog]\nroot = "{tmp_path / "catalog"}"\n')
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(_all(catalog))))
    spec = _spec(tmp_path, "headers", "paragraphs")
    code = main(["--config", str(config), "--json", "prepare", str(spec), "--files", "-"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert json.loads(out)["counts"]["samples"] == 3
