"""Selection specs: what a version draws, and running it again where the freeze ran it.

What carries the weight is the replay. A version records its spec and a
watermark, and whatever the catalog does afterwards — new samples, new
answers, a tombstone, a skip withdrawn, metadata rewritten in place, a
collection added — running the spec at that watermark gives the members
back. See docs/adr/0050.
"""

import json
from types import SimpleNamespace
from typing import ClassVar

import pytest
from sqlalchemy import create_engine, func, insert, select, text

from strata.catalog import Catalog, CatalogError, copy_index
from strata.catalog.index import tables as t
from strata.catalog.selection import SelectionSpec, compares, typed
from strata.catalog.sync.merge import MergeError, merge_annotations
from strata.catalog.verify import verify_dataset
from strata.contracts import Choices, ClassificationSchema, Text
from strata.contracts import sample_types as types


class Mail(Text):
    """A text with facts the type declares, as the email type's are."""

    segment: ClassVar[str] = "mail"

    class Metadata(Text.Metadata):
        word_count: int = 0
        html_tags: int = 0


class Image(types.SampleType):
    """Another type declaring word_count otherwise, to clash with mail."""

    media: ClassVar[str] = "image"
    segment: ClassVar[str] = "scan"

    class Metadata(types.SampleType.Metadata):
        word_count: str = ""


def _entry(name, obj):
    return SimpleNamespace(
        name=name,
        value=f"{obj.__module__}:{obj.__name__}",
        dist=SimpleNamespace(name="test-types", version="9.9"),
        load=lambda: obj,
    )


@pytest.fixture(autouse=True)
def installed(monkeypatch):
    builtin = types.entries()
    monkeypatch.setattr(
        types, "entries", lambda: [*builtin, _entry("mail", Mail), _entry("scan", Image)]
    )


COLLECTION = "phishnet/enron/single"


@pytest.fixture
def mail(catalog, tmp_path):
    """Six messages, four answered phish or ham, one flagged reject in qc."""
    root = tmp_path / "mail"
    root.mkdir()
    facts = [
        {"word_count": 120, "html_tags": 0, "folder": "inbox"},
        {"word_count": 5000, "html_tags": 0, "folder": "inbox"},
        {"word_count": 300, "html_tags": 4},
        {"word_count": 80, "html_tags": 0, "folder": "sent"},
        {"word_count": 90, "html_tags": 0},
        {"word_count": 70, "html_tags": 0},
    ]
    paths = []
    for i, _ in enumerate(facts):
        path = root / f"m{i}.txt"
        path.write_text(f"message {i}")
        paths.append(path)
    by_path = dict(zip(paths, facts, strict=True))
    ids = catalog.ingest(
        paths,
        media="text",
        subtype="mail",
        metadata_for=lambda p: by_path[p],
        collections=[COLLECTION],
    )
    emails = catalog.label_sets.create("emails", ClassificationSchema(classes=["phish", "ham"]))
    qc = catalog.label_sets.create("qc", ClassificationSchema(classes=["reject", "ok"]))
    catalog.annotations.annotate_many(
        emails, [(ids[i], Choices(values=["phish" if i % 2 else "ham"])) for i in range(5)]
    )
    catalog.annotations.annotate(ids[3], qc, Choices(values=["reject"]))
    return SimpleNamespace(ids=ids, emails=emails, qc=qc, root=root)


def spec(**select) -> SelectionSpec:
    return SelectionSpec.model_validate(
        {"select": {"types": ["mail"], "label_set": "emails", **select}}
    )


def members(catalog, dataset_id) -> set[int]:
    with catalog.engine.connect() as conn:
        return set(
            conn.execute(
                select(t.dataset_member.c.sample_id).where(
                    t.dataset_member.c.dataset_id == dataset_id
                )
            ).scalars()
        )


SFT = """
[select]
types       = ["mail"]
collections = ["phishnet/enron/single"]
label_set   = "emails"
sources     = ["human"]

[select.where]
all = [
  { key = "html_tags",  op = "==", value = 0 },
  { key = "word_count", op = "<=", value = 2000 },
  { not = { label_set = "qc", asserts = "reject" } },
]
"""


# ----------------------------------------------------------------------
# What a spec may say
# ----------------------------------------------------------------------


def test_the_same_conditions_in_another_order_are_the_same_spec():
    one = SelectionSpec.from_toml(SFT)
    html = '{ key = "html_tags",  op = "==", value = 0 },'
    words = '{ key = "word_count", op = "<=", value = 2000 },'
    other = SelectionSpec.from_toml(SFT.replace(f"{html}\n  {words}", f"{words}\n  {html}"))
    assert other.model_dump() != one.model_dump()
    assert one.canonical() == other.canonical()
    assert one.spec_hash == other.spec_hash


def test_a_spec_names_its_types():
    with pytest.raises(CatalogError, match="types"):
        SelectionSpec.from_toml('[select]\nlabel_set = "emails"\n')


@pytest.mark.parametrize(
    "condition",
    [
        {"key": "word_count", "op": "~", "value": 1},
        {"key": "word_count", "op": "exists", "value": 1},
        {"key": "word_count", "op": "in", "value": 1},
        {"key": "word_count", "op": "<", "value": True},
        {"key": "word_count", "op": "==", "value": 1, "and": 2},
    ],
)
def test_a_condition_outside_the_format_is_refused(condition):
    with pytest.raises(ValueError):
        spec(where=condition)


def test_a_list_is_named_with_its_version():
    with pytest.raises(ValueError, match="version"):
        spec(lists={"exclude": ["near-duplicates"]})


def test_values_keep_their_type():
    condition = SelectionSpec.model_validate(
        {
            "select": {
                "types": ["mail"],
                "label_set": "e",
                "where": {"key": "n", "op": "==", "value": 2000},
            }
        }
    ).select.where
    assert compares(condition, {"n": 2000})
    assert not compares(condition, {"n": "2000"})
    assert typed("2000") == 2000 and typed('"2000"') == "2000" and typed("abc") == "abc"


def test_a_missing_key_fails_every_comparison_but_exists():
    for op, value in [("==", 1), ("!=", 1), ("<", 1), ("in", [1])]:
        condition = spec(where={"key": "k", "op": op, "value": value}).select.where
        assert not compares(condition, {})
    assert not compares(spec(where={"key": "k", "op": "exists"}).select.where, {})


# ----------------------------------------------------------------------
# What a freeze selects and records
# ----------------------------------------------------------------------


def test_a_freeze_selects_by_the_spec_and_records_it(catalog, mail):
    chosen = SelectionSpec.from_toml(SFT)
    dataset_id = catalog.create_dataset("sft", mail.emails, spec=chosen)
    # 1 too long, 2 has HTML, 3 rejected in qc, 5 unanswered
    assert members(catalog, dataset_id) == {mail.ids[0], mail.ids[4]}
    with catalog.engine.connect() as conn:
        row = conn.execute(select(t.dataset).where(t.dataset.c.id == dataset_id)).one()
    assert row.spec == chosen.canonical() and row.spec_hash == chosen.spec_hash
    assert set(row.watermark) == {"sample", "sample_collection", "annotation", "event"}
    counts = row.selection
    assert counts["candidates"] == 5 and counts["selected"] == 2
    assert counts["by_collection"] == {COLLECTION: 5}
    assert counts["by_type"] == {"text/mail": 5}
    assert sorted(entry["rejected"] for entry in counts["rejected"]) == [1, 1, 1]


def test_a_freeze_with_no_spec_records_the_one_it_ran(catalog, mail):
    dataset_id = catalog.create_dataset("all", mail.emails, collections=[COLLECTION])
    assert len(members(catalog, dataset_id)) == 5
    with catalog.engine.connect() as conn:
        recorded = conn.execute(
            select(t.dataset.c.spec).where(t.dataset.c.id == dataset_id)
        ).scalar_one()
    # The plain case of the media covers every subtype of it
    assert recorded["select"]["types"] == ["text"]
    assert recorded["select"]["collections"] == [COLLECTION]
    assert verify_dataset(catalog, "all", 1).ok


def test_a_type_covers_its_subtypes(catalog, mail):
    as_text = catalog.create_dataset("t", mail.emails, spec=spec(types=["text"]))
    as_mail = catalog.create_dataset("m", mail.emails, spec=spec())
    assert members(catalog, as_text) == members(catalog, as_mail)


def test_a_key_no_candidate_carries_is_refused_with_the_keys_that_do(catalog, mail):
    with pytest.raises(CatalogError, match=r"'foldr'.*folder"):
        catalog.create_dataset(
            "x", mail.emails, spec=spec(where={"key": "foldr", "op": "==", "value": "inbox"})
        )


def test_an_undeclared_key_that_occurs_is_allowed_and_its_absence_counted(catalog, mail):
    dataset_id = catalog.create_dataset(
        "x", mail.emails, spec=spec(where={"key": "folder", "op": "==", "value": "inbox"})
    )
    assert members(catalog, dataset_id) == {mail.ids[0], mail.ids[1]}
    with catalog.engine.connect() as conn:
        counts = conn.execute(
            select(t.dataset.c.selection).where(t.dataset.c.id == dataset_id)
        ).scalar_one()
    assert counts["missing"] == {"folder": 2}


def test_a_declared_key_compared_with_the_wrong_type_is_refused(catalog, mail):
    with pytest.raises(CatalogError, match="declared a number"):
        catalog.create_dataset(
            "x", mail.emails, spec=spec(where={"key": "word_count", "op": "==", "value": "120"})
        )


def test_two_types_declaring_one_key_differently_are_refused_together(catalog, mail):
    with pytest.raises(CatalogError, match="'mail' and 'scan'"):
        catalog.create_dataset("x", mail.emails, spec=spec(types=["mail", "scan"]))


def test_sources_say_which_answers_count(catalog, mail):
    catalog.annotations.annotate(mail.ids[5], mail.emails, Choices(values=["ham"]), source=t.MODEL)
    everyone = catalog.create_dataset("a", mail.emails, spec=spec())
    people = catalog.create_dataset("p", mail.emails, spec=spec(sources=["human"]))
    assert members(catalog, everyone) - members(catalog, people) == {mail.ids[5]}


def test_any_and_not_combine(catalog, mail):
    either = {
        "any": [
            {"key": "word_count", "op": ">", "value": 1000},
            {"label_set": "qc", "asserts": "reject"},
        ]
    }
    dataset_id = catalog.create_dataset("x", mail.emails, spec=spec(where={"not": either}))
    assert members(catalog, dataset_id) == {mail.ids[0], mail.ids[2], mail.ids[4]}


def test_a_spec_over_another_label_set_than_the_freeze_is_refused(catalog, mail):
    with pytest.raises(CatalogError, match="'emails'"):
        catalog.create_dataset("x", mail.qc, spec=spec())


def test_a_spec_choosing_nothing_says_what_rejected_what(catalog, mail):
    with pytest.raises(CatalogError, match=r"none passed.*rejected by"):
        catalog.create_dataset(
            "x", mail.emails, spec=spec(where={"key": "word_count", "op": ">", "value": 10**6})
        )


def test_the_same_spec_again_is_the_same_version_and_another_is_not(catalog, mail):
    first = catalog.create_dataset("x", mail.emails, spec=spec())
    assert catalog.create_dataset("x", mail.emails, spec=spec()) == first
    # The same members today, by another rule
    wider = spec(where={"key": "word_count", "op": "<", "value": 10**6})
    assert catalog.create_dataset("x", mail.emails, spec=wider) != first


# ----------------------------------------------------------------------
# Lists
# ----------------------------------------------------------------------


def _checksums(catalog, ids) -> list[str]:
    with catalog.engine.connect() as conn:
        return list(
            conn.execute(select(t.sample.c.checksum).where(t.sample.c.id.in_(ids))).scalars()
        )


def test_a_list_version_never_changes(catalog, mail):
    one, new = catalog.lists.put("dups", _checksums(catalog, mail.ids[:1]))
    again, again_new = catalog.lists.put("dups", _checksums(catalog, mail.ids[:1]))
    other, _ = catalog.lists.put("dups", _checksums(catalog, mail.ids[:2]))
    assert (one.version, new, again.version, again_new, other.version) == (1, True, 1, False, 2)
    assert catalog.lists.get("dups", 1)[1] == sorted(_checksums(catalog, mail.ids[:1]))


def test_a_list_excludes_or_includes_by_checksum(catalog, mail):
    catalog.lists.put("dups", _checksums(catalog, [mail.ids[0]]))
    catalog.lists.put("picked", _checksums(catalog, [mail.ids[0], mail.ids[4]]))
    dropped = catalog.create_dataset("d", mail.emails, spec=spec(lists={"exclude": ["dups@1"]}))
    picked = catalog.create_dataset("p", mail.emails, spec=spec(lists={"include": ["picked@1"]}))
    assert mail.ids[0] not in members(catalog, dropped) and len(members(catalog, dropped)) == 4
    assert members(catalog, picked) == {mail.ids[0], mail.ids[4]}
    with catalog.engine.connect() as conn:
        counts = conn.execute(
            select(t.dataset.c.selection).where(t.dataset.c.id == dropped)
        ).scalar_one()
    assert counts["lists"] == {"dups@1": {"removed": 1}}


def test_a_list_the_catalog_lacks_is_refused(catalog, mail):
    with pytest.raises(CatalogError, match="dups@1"):
        catalog.create_dataset("d", mail.emails, spec=spec(lists={"exclude": ["dups@1"]}))


# ----------------------------------------------------------------------
# Running it again
# ----------------------------------------------------------------------


def test_a_spec_run_again_at_its_watermark_gives_the_members_back(catalog, mail, tmp_path):
    catalog.create_dataset("sft", mail.emails, spec=SelectionSpec.from_toml(SFT))
    assert verify_dataset(catalog, "sft", 1).ok

    # Everything that would change what the spec selects now
    extra = tmp_path / "late.txt"
    extra.write_text("late")
    [late] = catalog.ingest(
        [extra], media="text", subtype="mail", metadata={"word_count": 1, "html_tags": 0},
        collections=[COLLECTION],
    )  # fmt: skip
    catalog.annotations.annotate(late, mail.emails, Choices(values=["ham"]))
    catalog.annotations.annotate(mail.ids[5], mail.emails, Choices(values=["ham"]))
    catalog.annotations.annotate(mail.ids[3], mail.qc, Choices(values=["ok"]))
    catalog.samples.tombstone([mail.ids[0]])
    catalog.annotations.skip(mail.ids[4], mail.emails)
    catalog.annotations.unskip(mail.emails, [mail.ids[4]])
    with catalog.engine.begin() as conn:
        catalog.samples.describe(conn, mail.ids[1], metadata={"word_count": 10, "html_tags": 0})
        catalog.samples.collect(conn, [mail.ids[2]], ["elsewhere"])

    now = catalog.create_dataset("sft", mail.emails, spec=SelectionSpec.from_toml(SFT))
    assert members(catalog, now) == {mail.ids[1], mail.ids[3], mail.ids[5], late}
    report = verify_dataset(catalog, "sft", 1)
    assert report.ok, report.checks
    assert verify_dataset(catalog, "sft", 2).ok


def test_a_collection_added_later_is_not_in_the_replay(catalog, mail):
    with catalog.engine.begin() as conn:
        catalog.samples.collect(conn, mail.ids[:2], ["elsewhere"])
    first = catalog.create_dataset("x", mail.emails, spec=spec(collections=["elsewhere"]))
    # Answered at the freeze, and only in the collection after it
    with catalog.engine.begin() as conn:
        catalog.samples.collect(conn, [mail.ids[2]], ["elsewhere"])
    assert members(catalog, first) == set(mail.ids[:2])
    assert verify_dataset(catalog, "x", 1).ok
    later = catalog.create_dataset("x", mail.emails, spec=spec(collections=["elsewhere"]))
    assert members(catalog, later) == set(mail.ids[:3])


def test_a_tampered_membership_does_not_hold(catalog, mail):
    dataset_id = catalog.create_dataset("x", mail.emails, spec=spec())
    with catalog.engine.begin() as conn:
        conn.execute(
            t.dataset_member.delete().where(
                t.dataset_member.c.dataset_id == dataset_id,
                t.dataset_member.c.sample_id == mail.ids[0],
            )
        )
    report = verify_dataset(catalog, "x", 1)
    assert not report.ok
    assert f"selected and not members: [{mail.ids[0]}]" in report.checks[1].detail


def test_an_answer_that_arrived_superseded_was_never_the_answer(catalog, mail):
    catalog.create_dataset("x", mail.emails, spec=spec())
    # History carried from another catalog lands after the current answer
    with catalog.engine.begin() as conn:
        old = conn.execute(
            insert(t.annotation).values(
                sample_id=mail.ids[0], label_set_id=mail.emails, state=t.SKIPPED,
                source=t.HUMAN, superseded_at=func.now(),
            )
        ).inserted_primary_key[0]  # fmt: skip
        conn.execute(
            insert(t.event).values(kind=t.ARRIVED, sample_id=mail.ids[0], annotation_id=old)
        )
    again = catalog.create_dataset("x", mail.emails, spec=spec())
    assert mail.ids[0] in members(catalog, again)


def test_a_version_with_no_spec_says_it_cannot_be_verified(catalog, mail):
    catalog.create_dataset("x", mail.emails, sample_ids=mail.ids[:2])
    with pytest.raises(CatalogError, match="records no selection spec"):
        verify_dataset(catalog, "x", 1)


def test_a_superseded_answers_classes_stay_readable(catalog, mail):
    catalog.annotations.annotate(mail.ids[3], mail.qc, Choices(values=["ok"]))
    with catalog.engine.connect() as conn:
        rows = conn.execute(
            select(t.annotation.c.superseded_at.is_(None), t.annotation_class.c.class_name)
            .join(t.annotation_class, t.annotation_class.c.annotation_id == t.annotation.c.id)
            .where(t.annotation.c.sample_id == mail.ids[3], t.annotation.c.label_set_id == mail.qc)
        ).all()
    assert sorted(rows) == [(False, "reject"), (True, "ok")]
    assert [s.id for s in catalog.samples.with_class(mail.qc, "ok", "*")] == [mail.ids[3]]
    assert catalog.samples.with_class(mail.qc, "reject", "*") == []


def test_matching_compares_typed_values(catalog, mail):
    assert {r.id for r in catalog.samples.matching("*", {"word_count": 120})} == {mail.ids[0]}
    assert catalog.samples.matching("*", {"word_count": "120"}) == []


# ----------------------------------------------------------------------
# Locking, and moving with the catalog
# ----------------------------------------------------------------------


def test_a_freeze_holds_writers_back_while_it_reads(catalog, mail, tmp_path):
    from strata.catalog.index.locks import lock

    other = create_engine(
        f"sqlite:///{tmp_path / 'catalog' / 'catalog.db'}", connect_args={"timeout": 0.1}
    )
    with catalog.engine.begin() as conn:
        lock(conn)
        with pytest.raises(Exception, match="locked"), other.begin() as writer:
            writer.execute(text("UPDATE sample SET metadata = metadata"))


def test_a_copy_verifies_where_the_original_did(catalog, mail, tmp_path):
    catalog.lists.put("dups", _checksums(catalog, [mail.ids[0]]))
    catalog.samples.tombstone([mail.ids[5]])
    catalog.create_dataset("x", mail.emails, spec=spec(lists={"exclude": ["dups@1"]}))
    copy = Catalog.create(f"sqlite:///{tmp_path / 'copy.db'}", catalog.blobs)
    copy_index(catalog, copy)
    assert verify_dataset(copy, "x", 1).ok
    assert [row.ref for row in copy.lists.every()] == ["dups@1"]


def test_a_merge_carries_lists_and_refuses_one_held_otherwise(catalog, mail, tmp_path):
    copy = Catalog.create(f"sqlite:///{tmp_path / 'copy.db'}", catalog.blobs)
    copy_index(catalog, copy)
    copy.lists.put("dups", _checksums(catalog, [mail.ids[0]]))
    report = merge_annotations(copy, catalog)
    assert report.lists == ["dups@1"]
    assert catalog.lists.get("dups", 1)[1] == copy.lists.get("dups", 1)[1]

    copy.lists.put("other", _checksums(catalog, [mail.ids[1]]))
    catalog.lists.put("other", _checksums(catalog, [mail.ids[2]]))
    with pytest.raises(MergeError, match="other@1"):
        merge_annotations(copy, catalog)


def test_the_counts_are_json(catalog, mail):
    dataset_id = catalog.create_dataset("x", mail.emails, spec=SelectionSpec.from_toml(SFT))
    with catalog.engine.connect() as conn:
        counts = conn.execute(
            select(t.dataset.c.selection).where(t.dataset.c.id == dataset_id)
        ).scalar_one()
    assert json.loads(json.dumps(counts)) == counts


def test_the_values_of_an_in_are_a_set():
    one = spec(where={"key": "folder", "op": "in", "value": ["inbox", "sent"]})
    other = spec(where={"key": "folder", "op": "in", "value": ["sent", "inbox", "inbox"]})
    mixed = spec(where={"key": "folder", "op": "in", "value": ["sent", 3, "inbox", 3]})
    assert one.spec_hash == other.spec_hash
    assert one.canonical() == other.canonical()
    assert mixed.canonical()["select"]["where"]["value"] == ["inbox", "sent", 3]
