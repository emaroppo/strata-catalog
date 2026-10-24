"""Answers carried between two catalogs built apart.

What matters most is what an import must not do: lose a person's answer's
history, overrule what the target already knows, store a span on text it
no longer points at, or write anything twice when it is run again.
"""

import hashlib
import json

import pytest

from strata.catalog import Catalog
from strata.catalog.rows import CatalogError
from strata.catalog.transfer import AnswersFile, ExportedRow, ExportedSample, read_pairs
from strata.catalog.verify import verify_run
from strata.contracts import Span, Spans, SpanSchema

SCHEMA = SpanSchema(classes=["PER", "ORG"])
OLD = "Ada met Bob at the Society.\n"
#: The same message prepared again: one word gone, so every offset after it moved
NEW = "Ada met Bob at Society.\n"


def _with(tmp_path, name: str, texts: list[str]) -> tuple[Catalog, list[int]]:
    catalog = Catalog.local(tmp_path / name)
    root = tmp_path / f"raw-{name}"
    root.mkdir()
    paths = []
    for i, text in enumerate(texts):
        path = root / f"{i}.txt"
        path.write_text(text)
        paths.append(path)
    return catalog, catalog.ingest(paths, media="text")


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _spans(text: str, *words: tuple[str, str]) -> Spans:
    return Spans(
        values=[
            Span(labels=[label], start=text.index(w), end=text.index(w) + len(w), text=w)
            for w, label in words
        ]
    )


@pytest.fixture
def source(tmp_path):
    """A catalog where a person answered twice, correcting themselves."""
    catalog, [one, two] = _with(tmp_path, "source", [OLD, "Nothing here.\n"])
    set_id = catalog.label_sets.create("entities", SCHEMA)
    catalog.annotations.annotate(one, set_id, _spans(OLD, ("Ada", "PER")), batch="gold")
    catalog.annotations.annotate(
        one, set_id, _spans(OLD, ("Ada", "PER"), ("Bob", "PER"), ("Society", "ORG"))
    )
    catalog.annotations.skip(two, set_id)
    return catalog


def _export(source) -> AnswersFile:
    # Through the file, as the command goes, so the round trip is what is tested
    text = source.export_answers("entities", ["human"]).model_dump_json(by_alias=True)
    return AnswersFile.model_validate_json(text)


def test_an_answer_arrives_with_its_history_under_one_run(tmp_path, source):
    target, [one, two] = _with(tmp_path, "target", [OLD, "Nothing here.\n"])
    report = target.import_answers(_export(source))

    assert (report.matched, report.copied, report.rows, report.history) == (2, 2, 3, 1)
    set_id, _ = target.label_sets.get("entities")
    history = target.annotations.history(one, set_id)
    assert [len(a.value.values) for a in history if a.value] == [1, 3]
    assert history[0].batch == "gold" and history[0].superseded_at is not None
    assert history[1].batch == "gold"  # carried onto the row that corrected it
    assert target.annotations.history(two, set_id)[0].state == "skipped"

    check = verify_run(target, report.run_id)
    assert check.ok, check.checks


def test_run_again_it_writes_nothing(tmp_path, source):
    target, _ = _with(tmp_path, "target", [OLD, "Nothing here.\n"])
    answers = _export(source)
    target.import_answers(answers)
    again = target.import_answers(answers)
    assert (again.rows, again.already) == (0, 3)


def test_a_changed_sample_is_paired_and_its_spans_follow_the_text(tmp_path, source):
    target, [one] = _with(tmp_path, "target", [NEW])
    answers = _export(source)
    old = next(s.checksum for s in answers.samples if s.text == OLD)
    report = target.import_answers(answers, pairs={old: _checksum(NEW)})

    assert (report.paired, report.unmatched) == (1, 1)
    set_id, _ = target.label_sets.get("entities")
    value = target.annotations.annotation_of(one, set_id)
    assert isinstance(value, Spans)
    assert [(s.text, NEW[s.start : s.end]) for s in value.values] == [
        ("Ada", "Ada"),
        ("Bob", "Bob"),
        ("Society", "Society"),
    ]


def test_a_span_whose_text_is_gone_is_counted_not_stored(tmp_path, source):
    target, [one] = _with(tmp_path, "target", ["Ada met Bob.\n"])
    answers = _export(source)
    old = next(s.checksum for s in answers.samples if s.text == OLD)
    report = target.import_answers(answers, pairs={old: _checksum("Ada met Bob.\n")})
    # Society is lost from the current answer; the history's one span survives
    assert report.spans_lost == 1
    set_id, _ = target.label_sets.get("entities")
    value = target.annotations.annotation_of(one, set_id)
    assert isinstance(value, Spans)
    assert [s.text for s in value.values] == ["Ada", "Bob"]


def test_an_equal_answer_that_disagrees_is_a_dispute_the_target_keeps_its_side_of(tmp_path, source):
    target, [one, _] = _with(tmp_path, "target", [OLD, "Nothing here.\n"])
    set_id = target.label_sets.create("entities", SCHEMA)
    mine = _spans(OLD, ("Bob", "PER"))
    target.annotations.annotate(one, set_id, mine)

    report = target.import_answers(_export(source))

    assert report.conflicted == 1
    assert target.annotations.annotation_of(one, set_id) == mine
    assert target.conflicts.disputed(set_id, "*")


def test_a_skip_never_replaces_an_answer(tmp_path, source):
    target, [_, two] = _with(tmp_path, "target", [OLD, "Nothing here.\n"])
    set_id = target.label_sets.create("entities", SCHEMA)
    target.annotations.annotate(two, set_id, Spans(values=[]), source="import")
    report = target.import_answers(_export(source))
    assert report.skips_ignored == 1


def test_a_skip_made_afresh_lands_once(tmp_path):
    # Skips written by a rule carry no time of their own, so a second run
    # cannot know them by it; a skip on a skip still changes nothing
    target, [one] = _with(tmp_path, "target", [OLD])
    set_id = target.label_sets.create("entities", SCHEMA)
    answers = AnswersFile(
        catalog_id="rules/html",
        label_set="entities",
        schema=SCHEMA.model_dump(mode="json"),
        samples=[
            ExportedSample(
                checksum=_checksum(OLD), rows=[ExportedRow(state="skipped", source="import")]
            )
        ],
    )
    assert target.import_answers(answers).copied == 1
    again = target.import_answers(answers)
    assert (again.agreed, again.rows) == (1, 0)
    assert len(target.annotations.history(one, set_id)) == 1


def test_a_label_set_with_another_schema_is_refused(tmp_path, source):
    target, _ = _with(tmp_path, "target", [OLD])
    target.label_sets.create("entities", SpanSchema(classes=["PER"]))
    with pytest.raises(CatalogError, match="another schema"):
        target.import_answers(_export(source))
    # Under a name of its own it lands
    assert target.import_answers(_export(source), into="entities-carried").matched == 1


def test_a_catalog_does_not_import_its_own_answers(source):
    with pytest.raises(CatalogError, match="merge"):
        source.import_answers(_export(source))


def test_a_dry_run_reports_and_keeps_nothing(tmp_path, source):
    target, _ = _with(tmp_path, "target", [OLD])
    report = target.import_answers(_export(source), dry_run=True)
    assert report.copied == 1
    assert target.runs.every() == []
    with pytest.raises(CatalogError):
        target.label_sets.get("entities")


def test_a_checksum_paired_twice_is_refused():
    with pytest.raises(CatalogError, match="paired twice"):
        read_pairs(["a b", "a c"])


def test_a_span_with_no_text_is_remapped_by_what_its_offsets_cover(tmp_path):
    # A span's text is a convenience, and some catalogs never stored it
    source, [one] = _with(tmp_path, "bare", [OLD])
    set_id = source.label_sets.create("entities", SCHEMA)
    start = OLD.index("Society")
    source.annotations.annotate(
        one, set_id, Spans(values=[Span(labels=["ORG"], start=start, end=start + 7, text="")])
    )
    target, [new] = _with(tmp_path, "target", [NEW])
    answers = AnswersFile.model_validate_json(
        source.export_answers("entities").model_dump_json(by_alias=True)
    )
    report = target.import_answers(answers, pairs={answers.samples[0].checksum: _checksum(NEW)})
    assert (report.spans_carried, report.spans_lost) == (1, 0)
    set_id, _ = target.label_sets.get("entities")
    value = target.annotations.annotation_of(new, set_id)
    assert isinstance(value, Spans)
    [span] = value.values
    assert NEW[span.start : span.end] == "Society"


# ----------------------------------------------------------------------
# A sample is its bytes and its type
# ----------------------------------------------------------------------


def _typed(tmp_path, name: str, subtypes: list[str]) -> tuple[Catalog, list[int]]:
    """OLD catalogued once for each of ``subtypes``."""
    catalog = Catalog.local(tmp_path / name)
    root = tmp_path / f"raw-{name}"
    root.mkdir()
    (root / "0.txt").write_text(OLD)
    ids = [
        catalog.ingest([root / "0.txt"], media="text", subtype=subtype)[0] for subtype in subtypes
    ]
    return catalog, ids


def _answered(tmp_path, subtype: str) -> AnswersFile:
    catalog, [one] = _typed(tmp_path, f"from-{subtype.replace('/', '-')}", [subtype])
    set_id = catalog.label_sets.create("entities", SCHEMA)
    catalog.annotations.annotate(one, set_id, _spans(OLD, ("Ada", "PER")))
    return _export(catalog)


def test_an_export_names_each_samples_type(tmp_path):
    answers = _answered(tmp_path, "sft/x")
    assert [s.type for s in answers.samples] == ["text/sft/x"]


def test_an_answer_lands_on_the_sample_of_its_type(tmp_path):
    # The same bytes as two types are two samples. docs/adr/0052
    target, [email, sft] = _typed(tmp_path, "target", ["email", "sft/x"])
    report = target.import_answers(_answered(tmp_path, "sft/x"))

    set_id, _ = target.label_sets.get("entities")
    assert (report.matched, report.unmatched) == (1, 0)
    assert target.annotations.annotation_of(sft, set_id) is not None
    assert target.annotations.annotation_of(email, set_id) is None


def test_an_answer_whose_type_the_target_lacks_is_unmatched(tmp_path):
    target, _ = _typed(tmp_path, "target", ["email"])
    report = target.import_answers(_answered(tmp_path, "sft/x"))
    assert (report.matched, report.unmatched, report.ambiguous) == (0, 1, 0)
    assert report.unmatched_checksums == [_checksum(OLD)]
    assert verify_run(target, report.run_id).ok


def test_an_answer_exported_without_a_type_lands_only_where_one_type_holds_its_bytes(tmp_path):
    # A file written before samples carried a type
    written = json.loads(_answered(tmp_path, "email").model_dump_json(by_alias=True))
    for sample in written["samples"]:
        del sample["type"]
    untyped = AnswersFile.model_validate(written)

    one, _ = _typed(tmp_path, "one", ["email"])
    assert one.import_answers(untyped).matched == 1

    two, _ = _typed(tmp_path, "two", ["email", "sft/x"])
    report = two.import_answers(untyped)
    assert (report.matched, report.unmatched, report.ambiguous) == (0, 1, 1)


def test_a_paired_answer_lands_on_the_successor_of_its_type(tmp_path):
    answers = _answered(tmp_path, "sft/x")
    target = Catalog.local(tmp_path / "target")
    root = tmp_path / "raw-target"
    root.mkdir()
    (root / "0.txt").write_text(NEW)
    [email] = target.ingest([root / "0.txt"], media="text", subtype="email")
    [sft] = target.ingest([root / "0.txt"], media="text", subtype="sft/x")

    report = target.import_answers(answers, pairs={_checksum(OLD): _checksum(NEW)})

    set_id, _ = target.label_sets.get("entities")
    assert report.paired == 1
    value = target.annotations.annotation_of(sft, set_id)
    assert isinstance(value, Spans) and [s.text for s in value.values] == ["Ada"]
    assert target.annotations.annotation_of(email, set_id) is None
