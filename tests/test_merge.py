"""Bringing a laptop's answers home.

The interesting cases are all the ones where the two sides know different
things. A merge that only handled agreement would be a copy, and a merge
that resolved disagreement by picking one would quietly destroy work
someone did on a train.
"""

import pytest

from strata.catalog import (
    Catalog,
    MergeError,
    copy_index,
    merge_annotations,
)
from strata.labels import BBoxSchema, Box, Boxes, Choices, ClassificationSchema


def _catalog(tmp_path, name, files=("a.jpg", "b.jpg", "c.jpg")):
    """A catalog holding three samples, and their ids.

    A copy preserves ids, so one set of ids addresses both sides of a merge
    — which is what makes these tests short, and is also the property the
    merge deliberately does not rely on.
    """
    catalog = Catalog.local(tmp_path / name)
    root = tmp_path / f"raw-{name}"
    root.mkdir(exist_ok=True)
    paths = []
    for f in files:
        path = root / f
        path.write_bytes(f"bytes of {f}".encode())
        paths.append(path)
    return catalog, catalog.ingest(paths, media="image")


@pytest.fixture
def pair(tmp_path):
    """A main catalog and a copy of it, as a laptop would take one."""
    main, ids = _catalog(tmp_path, "main")
    main.label_sets.create("demo", ClassificationSchema(classes=["cat", "dog"]))
    laptop = Catalog.local(tmp_path / "laptop")
    copy_index(main, laptop)
    return main, laptop, ids


# ----------------------------------------------------------------------
# The three outcomes
# ----------------------------------------------------------------------


def test_an_answer_the_target_lacks_is_copied(pair):
    main, laptop, ids = pair
    set_id, _ = laptop.label_sets.get("demo")
    laptop.annotations.annotate(ids[0], set_id, Choices(values=["cat"]))

    report = merge_annotations(laptop, main)

    main_set, _ = main.label_sets.get("demo")
    assert report.copied == 1
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["cat"])


def test_the_same_answer_twice_is_not_a_conflict(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]))
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["cat"]))

    report = merge_annotations(laptop, main)

    # Two people agreeing is the common case and must stay silent, or the
    # conflict queue is noise and nobody reads it
    assert (report.agreed, report.conflicted, report.copied) == (1, 0, 0)
    assert main.annotations.conflicts(main_set, "*") == []


def test_disagreement_keeps_both_and_settles_neither(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]))
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["dog"]))

    report = merge_annotations(laptop, main)

    assert report.conflicted == 1
    # The target keeps what it had — a merge cannot judge between two people
    # who each looked at the sample
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["cat"])
    [conflict] = main.annotations.conflicts(main_set, "*")
    assert conflict["kept"] == Choices(values=["cat"])
    assert conflict["other"] == Choices(values=["dog"])
    assert conflict["origin"] == laptop.id


# ----------------------------------------------------------------------
# What is refused
# ----------------------------------------------------------------------


def test_two_unrelated_catalogs_are_refused(tmp_path):
    one, _ = _catalog(tmp_path, "one")
    two, _ = _catalog(tmp_path, "two")
    # Same bytes on both sides, so content addressing would happily match
    # them — and the ids behind those matches mean different things
    with pytest.raises(MergeError, match="different catalogs"):
        merge_annotations(one, two)


def test_a_dry_run_reports_without_writing(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    first, second = ids[0], ids[1]
    main.annotations.annotate(first, main_set, Choices(values=["cat"]))
    laptop.annotations.annotate(first, laptop_set, Choices(values=["dog"]))
    laptop.annotations.annotate(second, laptop_set, Choices(values=["cat"]))

    report = merge_annotations(laptop, main, dry_run=True)

    assert (report.conflicted, report.copied) == (1, 1)
    # Nothing moved: the count is what you look at before deciding
    assert main.annotations.annotation_of(second, main_set) is None
    assert main.annotations.conflicts(main_set, "*") == []


def test_an_unknown_label_set_is_reported_not_invented(pair):
    main, laptop, ids = pair
    other = laptop.label_sets.create("elsewhere", ClassificationSchema(classes=["x"]))
    laptop.annotations.annotate(ids[0], other, Choices(values=["x"]))

    report = merge_annotations(laptop, main)

    assert report.unknown_label_sets == ["elsewhere"]
    with pytest.raises(Exception, match="No label set"):
        main.label_sets.get("elsewhere")


def test_an_answer_for_a_sample_the_target_lacks_is_reported(tmp_path):
    main, _ = _catalog(tmp_path, "main")
    main.label_sets.create("demo", ClassificationSchema(classes=["cat", "dog"]))
    laptop = Catalog.local(tmp_path / "laptop")
    copy_index(main, laptop)

    # Ingested offline, after the copy: the main catalog has never seen it
    root = tmp_path / "extra"
    root.mkdir()
    new = root / "d.jpg"
    new.write_bytes(b"a sample from the field")
    [new_id] = laptop.ingest([new], media="image")
    laptop_set, _ = laptop.label_sets.get("demo")
    laptop.annotations.annotate(new_id, laptop_set, Choices(values=["dog"]))

    report = merge_annotations(laptop, main)

    # Named, not counted: the answer is still on the laptop, and a checksum
    # is what lets someone go and find the file it belongs to
    assert len(report.unknown_samples) == 1
    assert report.copied == 0


# ----------------------------------------------------------------------
# States that are not a plain answer
# ----------------------------------------------------------------------


def test_a_skip_is_copied_where_the_target_knows_nothing(pair):
    main, laptop, ids = pair
    laptop_set, _ = laptop.label_sets.get("demo")
    laptop.annotations.skip(ids[0], laptop_set)

    report = merge_annotations(laptop, main)

    main_set, _ = main.label_sets.get("demo")
    assert report.skipped == 1
    assert [s.id for s in main.samples.skipped(main_set, "*")] == [ids[0]]


def test_an_answer_beats_a_skip(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.skip(ids[0], main_set)
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["cat"]))

    report = merge_annotations(laptop, main)

    # Someone got further with it than the person who passed. That is not a
    # disagreement, and queueing it as one would waste a reviewer's time.
    assert (report.copied, report.conflicted) == (1, 0)
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["cat"])


def test_a_skip_does_not_overwrite_an_answer(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]))
    laptop.annotations.skip(ids[0], laptop_set)

    merge_annotations(laptop, main)

    # The reverse of the case above, and the destructive one: a skip landing
    # on an answer would discard work rather than add to it
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["cat"])


# ----------------------------------------------------------------------
# Not only classification
# ----------------------------------------------------------------------


def test_boxes_merge_and_disagree_like_anything_else(tmp_path):
    main, ids = _catalog(tmp_path, "main")
    main.label_sets.create("boxes", BBoxSchema(classes=["cat"]))
    laptop = Catalog.local(tmp_path / "laptop")
    copy_index(main, laptop)

    main_set, _ = main.label_sets.get("boxes")
    laptop_set, _ = laptop.label_sets.get("boxes")
    first, second = ids[0], ids[1]
    here = Boxes(values=[Box(label="cat", x=0.1, y=0.1, width=0.2, height=0.2)])
    there = Boxes(values=[Box(label="cat", x=0.5, y=0.5, width=0.2, height=0.2)])
    main.annotations.annotate(first, main_set, here)
    laptop.annotations.annotate(first, laptop_set, there)
    laptop.annotations.annotate(second, laptop_set, there)

    report = merge_annotations(laptop, main)

    # The merge reads values through the discriminated union, so a detection
    # catalog is not a case someone has to remember to add later
    assert (report.copied, report.conflicted) == (1, 1)
    assert main.annotations.annotation_of(second, main_set) == there
    [conflict] = main.annotations.conflicts(main_set, "*")
    assert conflict["other"] == there


# ----------------------------------------------------------------------
# Running it twice
# ----------------------------------------------------------------------


def test_merging_twice_changes_nothing_the_second_time(pair):
    main, laptop, ids = pair
    laptop_set, _ = laptop.label_sets.get("demo")
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["cat"]))

    merge_annotations(laptop, main)
    second = merge_annotations(laptop, main)

    # A merge interrupted halfway is re-run, so the second pass has to be
    # quiet rather than turn every copied answer into a conflict with itself
    assert (second.copied, second.conflicted) == (0, 0)
    assert second.agreed == 1


# ----------------------------------------------------------------------
# Where an answer came from travels with it
# ----------------------------------------------------------------------


def test_an_import_comes_home_as_an_import(pair):
    """Written as a person's, it would read as reviewed when nobody looked."""
    main, laptop, ids = pair
    laptop_set, _ = laptop.label_sets.get("demo")
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["cat"]), source="import")

    merge_annotations(laptop, main)

    main_set, _ = main.label_sets.get("demo")
    # Discarding by source is the public way to ask what a row's source is
    assert main.annotations.discard(main_set, "import") == 1


def test_a_person_there_supersedes_an_import_here(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]), source="import")
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["dog"]))

    report = merge_annotations(laptop, main)

    # Not a conflict: a person against a guess is not two people disagreeing
    assert (report.superseded, report.conflicted) == (1, 0)
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["dog"])
    assert main.annotations.conflicts(main_set, "*") == []
    assert main.annotations.discard(main_set, "import") == 0


def test_an_import_there_leaves_a_person_here_alone(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]))
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["dog"]), source="import")

    report = merge_annotations(laptop, main)

    assert (report.outranked, report.conflicted) == (1, 0)
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["cat"])
    assert main.annotations.conflicts(main_set, "*") == []


def test_a_person_confirming_an_import_raises_its_standing(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]), source="import")
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["cat"]))

    report = merge_annotations(laptop, main)

    assert (report.confirmed, report.agreed) == (1, 0)
    # Now a person's answer, so no longer something an import discard removes
    assert main.annotations.discard(main_set, "import") == 0
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["cat"])


def test_an_import_does_not_beat_a_persons_skip(pair):
    """An answer beats a skip because someone got further — an import got nowhere."""
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.skip(ids[0], main_set)
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["cat"]), source="import")

    report = merge_annotations(laptop, main)

    assert (report.outranked, report.copied) == (1, 0)
    assert [s.id for s in main.samples.skipped(main_set, "*")] == [ids[0]]


def test_two_imports_that_disagree_are_a_conflict(pair):
    """Equal standing, so neither may decide: the same rule as two people."""
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]), source="import")
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["dog"]), source="import")

    report = merge_annotations(laptop, main)

    assert report.conflicted == 1
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["cat"])


def test_a_dry_run_counts_what_the_ranking_would_do_and_writes_nothing(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]), source="import")
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["dog"]))

    report = merge_annotations(laptop, main, dry_run=True)

    assert report.superseded == 1
    assert main.annotations.annotation_of(ids[0], main_set) == Choices(values=["cat"])
    assert main.annotations.discard(main_set, "import") == 1


def test_a_superseded_import_is_quiet_the_second_time(pair):
    main, laptop, ids = pair
    main_set, _ = main.label_sets.get("demo")
    laptop_set, _ = laptop.label_sets.get("demo")
    main.annotations.annotate(ids[0], main_set, Choices(values=["cat"]), source="import")
    laptop.annotations.annotate(ids[0], laptop_set, Choices(values=["dog"]))

    merge_annotations(laptop, main)
    second = merge_annotations(laptop, main)

    assert (second.superseded, second.agreed, second.written) == (0, 1, 0)
