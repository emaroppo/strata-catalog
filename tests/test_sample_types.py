"""A sample is its bytes and its type.

The same bytes as two types are two samples, each with its own collections,
metadata and answers, over one stored blob. Every reference names both, a
lookup without the type refuses to guess, and a split keeps the two on one
side, since the same bytes on both sides is a leak. See docs/adr/0052.
"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from strata.catalog import EVERYTHING, AmbiguousSample, CatalogError, GivenSplit, LocalBackend
from strata.catalog.admin import remove
from strata.catalog.index import tables as t
from strata.contracts import Choices, Manifest


class CountingBackend(LocalBackend):
    """A local store that counts what it was asked to put."""

    def __init__(self, root):
        super().__init__(root)
        self.puts = 0

    def put(self, source, checksum):
        self.puts += 1
        return super().put(source, checksum)


@pytest.fixture
def counted(catalog, tmp_path):
    catalog.blobs = CountingBackend(tmp_path / "catalog" / "blobs")
    return catalog


def both(catalog, paths, **plain) -> tuple[list[int], list[int]]:
    """The files as plain images in ``photos`` and as frames in ``video``."""
    return (
        catalog.ingest(paths, media="image", collections=["photos"], **plain),
        catalog.ingest(paths, media="image", subtype="frames", collections=["video"]),
    )


def checksum_of(catalog, sample_id: int) -> str:
    with catalog.engine.connect() as conn:
        return conn.execute(
            select(t.sample.c.checksum).where(t.sample.c.id == sample_id)
        ).scalar_one()


def annotate_all(catalog, ids, label_set):
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids])


# ----------------------------------------------------------------------
# Two samples, one blob
# ----------------------------------------------------------------------


def test_the_same_bytes_as_two_types_are_two_samples_over_one_blob(counted, files, label_set):
    [plain], [frame] = both(counted, files(1))

    assert plain != frame
    assert counted.blobs.puts == 1
    [a] = counted.samples.matching(["photos"])
    [b] = counted.samples.matching(["video"])
    assert (a.id, a.type, b.id, b.type) == (plain, "image/plain", frame, "image/frames")
    assert a.checksum == b.checksum and a.location == b.location

    counted.annotations.annotate(plain, label_set, Choices(values=["cat"]))
    assert counted.annotations.annotation_of(frame, label_set) is None
    assert [s.id for s in counted.samples.unlabelled(label_set, EVERYTHING)] == [frame]


def test_ingesting_again_as_the_same_type_is_the_same_sample_with_its_metadata_updated(
    counted, files
):
    paths = files(2)
    first = counted.ingest(paths, media="image", subtype="frames")
    again = counted.ingest(paths, media="image", subtype="frames", metadata={"video": "v"})

    assert first == again and counted.blobs.puts == 2
    assert {s.metadata["video"] for s in counted.samples.matching(EVERYTHING)} == {"v"}


def test_the_second_type_reuses_a_raw_files_blob(counted, tmp_path):
    root = tmp_path / "maildir"
    root.mkdir()
    (root / "1.txt").write_text("one message")
    counted.ingest_raw(root, "enron")
    assert counted.blobs.puts == 1

    counted.ingest([root / "1.txt"], media="text", subtype="email")
    counted.ingest([root / "1.txt"], media="text", subtype="sft/x")
    assert counted.blobs.puts == 1


def test_the_unique_row_is_the_bytes_and_the_type(catalog, files):
    [plain], _ = both(catalog, files(1))
    row = catalog.samples.by_checksum(checksum_of(catalog, plain), "image/plain")
    assert row is not None
    with pytest.raises(IntegrityError), catalog.engine.begin() as conn:
        catalog.samples.add(
            conn, row.checksum, row.location, media="image", subtype="plain", metadata=None
        )


# ----------------------------------------------------------------------
# Looking a sample up
# ----------------------------------------------------------------------


def test_a_lookup_by_bytes_alone_refuses_to_guess_between_two_types(catalog, files):
    [plain], [frame] = both(catalog, files(1))
    checksum = checksum_of(catalog, plain)

    with pytest.raises(AmbiguousSample, match="image/frames, image/plain"):
        catalog.samples.by_checksum(checksum)
    assert catalog.samples.by_checksum(checksum, "image/plain").id == plain
    assert catalog.samples.by_checksum(checksum, "image/frames").id == frame
    assert catalog.samples.by_checksum(checksum, "image/scan") is None

    # One live sample with the bytes is unambiguous again
    catalog.samples.tombstone([frame])
    assert catalog.samples.by_checksum(checksum).id == plain


def test_removing_one_type_leaves_the_other_and_the_bytes(catalog, files, label_set):
    [plain], _ = both(catalog, files(1))
    catalog.annotations.annotate(plain, label_set, Choices(values=["cat"]))
    checksum = checksum_of(catalog, plain)

    with pytest.raises(CatalogError, match="several types"):
        remove(catalog, checksums=[checksum])
    report = remove(catalog, checksums=[checksum], types=["image/frames"])

    assert report.removed == 1
    row = catalog.samples.by_checksum(checksum)
    assert row is not None and row.id == plain
    assert catalog.blobs.get(row.location) == b"contents of img0"
    assert catalog.annotations.annotation_of(plain, label_set) == Choices(values=["cat"])


def test_the_cache_holds_one_file_for_two_types(catalog, files, tmp_path):
    [plain], _ = both(catalog, files(1))
    checksum = checksum_of(catalog, plain)
    paths = catalog.ensure_cached([checksum], tmp_path / "cache")
    assert list(paths) == [checksum] and paths[checksum].read_bytes() == b"contents of img0"


# ----------------------------------------------------------------------
# A freeze
# ----------------------------------------------------------------------


def sides_by_checksum(catalog, manifest: Manifest) -> dict[str, set[str]]:
    sides: dict[str, set[str]] = {}
    for sample in manifest.samples:
        sides.setdefault(sample.checksum, set()).add(sample.split)
    return sides


def frozen(catalog, label_set, root, name, **freeze) -> Manifest:
    dataset_id = catalog.create_dataset(name, label_set, collections=EVERYTHING, **freeze)
    out = catalog.materialise(dataset_id, root / name)
    return Manifest.model_validate_json((out / "manifest.json").read_text())


@pytest.mark.parametrize("seed", range(6))
def test_a_freeze_over_both_types_keeps_the_same_bytes_on_one_side(
    catalog, files, label_set, tmp_path, seed
):
    plain, frames = both(catalog, files(10))
    annotate_all(catalog, plain + frames, label_set)

    manifest = frozen(catalog, label_set, tmp_path, "d", val_ratio=0.3, seed=seed)

    assert len(manifest.samples) == 20
    assert all(len(sides) == 1 for sides in sides_by_checksum(catalog, manifest).values())
    assert {s.split for s in manifest.samples} >= {"train", "val"}


@pytest.mark.parametrize("seed", range(6))
def test_the_same_bytes_join_the_groups_a_key_makes(catalog, files, label_set, tmp_path, seed):
    # Grouped by video alone, each type's frame of one picture is in another
    # group, and the draw would be free to part them
    paths = files(8)
    plain = catalog.ingest(paths, media="image", metadata_for=lambda p: {"video": f"a{p.stem}"})
    frames = catalog.ingest(
        paths, media="image", subtype="frames", metadata_for=lambda p: {"video": f"b{p.stem}"}
    )
    annotate_all(catalog, plain + frames, label_set)

    manifest = frozen(
        catalog, label_set, tmp_path, "g", val_ratio=0.25, group_by="video", seed=seed
    )

    assert all(len(sides) == 1 for sides in sides_by_checksum(catalog, manifest).values())


def test_a_given_side_reaches_the_other_type_over_the_same_bytes(
    catalog, files, label_set, tmp_path
):
    paths = files(6)
    plain = catalog.ingest(
        paths, media="image", metadata_for=lambda p: {"set": "test" if p.stem < "img002" else ""}
    )
    frames = catalog.ingest(paths, media="image", subtype="frames")
    annotate_all(catalog, plain + frames, label_set)

    given = GivenSplit(key="set", holdout=["test"])
    manifest = frozen(catalog, label_set, tmp_path, "d", given=given)

    held = {s.id for s in manifest.samples if s.split == "holdout"}
    assert held == {*plain[:2], *frames[:2]}


def test_a_given_split_that_parts_the_same_bytes_is_refused(catalog, files, label_set):
    paths = files(6)
    plain = catalog.ingest(paths, media="image", metadata={"set": "test"})
    frames = catalog.ingest(paths, media="image", subtype="frames", metadata={"set": "dev"})
    annotate_all(catalog, plain + frames, label_set)

    given = GivenSplit(key="set", holdout=["test"], val=["dev"])
    with pytest.raises(CatalogError, match="same bytes cannot be on both sides"):
        catalog.create_dataset("d", label_set, collections=EVERYTHING, given=given)


def test_a_typed_list_fixes_the_side_of_its_own_type_and_the_bytes_go_with_it(catalog, files):
    plain, frames = both(catalog, files(6))
    checksums = [checksum_of(catalog, i) for i in plain[:2]]
    catalog.lists.put("dev-frames", checksums, "image/frames")
    catalog.lists.put("dev-any", checksums)
    by_frames = GivenSplit(val_lists=["dev-frames@1"])
    by_bytes = GivenSplit(val_lists=["dev-any@1"])

    # Over the plain images alone the frames' list names no member
    assert catalog.given_sides(by_frames, plain) == {}
    assert catalog.given_sides(by_bytes, plain) == dict.fromkeys(plain[:2], "val")
    # Over both, the frames it names take the plain images with their bytes
    expected = dict.fromkeys([*plain[:2], *frames[:2]], "val")
    assert catalog.given_sides(by_frames, plain + frames) == expected


def test_a_new_type_over_inherited_bytes_takes_their_side(catalog, files, label_set, tmp_path):
    plain = catalog.ingest(files(10), media="image")
    annotate_all(catalog, plain, label_set)
    first = frozen(catalog, label_set, tmp_path / "v1", "d")

    frames = catalog.ingest(files(10), media="image", subtype="frames")
    annotate_all(catalog, frames, label_set)
    second = frozen(catalog, label_set, tmp_path / "v2", "d")

    before = {s.checksum: s.split for s in first.samples}
    assert len(second.samples) == 20
    assert all(s.split == before[s.checksum] for s in second.samples)


# ----------------------------------------------------------------------
# A manifest
# ----------------------------------------------------------------------


def test_a_manifest_entry_names_its_type_and_two_types_share_one_file(
    catalog, files, label_set, tmp_path
):
    # The frames carry a source path with an extension and the plain rows
    # none, so naming each row by its own would write two files
    paths = files(4)
    plain = catalog.ingest(paths, media="image")
    frames = catalog.ingest(
        paths, media="image", subtype="frames", metadata_for=lambda p: {"source_path": p.name}
    )
    annotate_all(catalog, plain + frames, label_set)

    manifest = frozen(catalog, label_set, tmp_path, "d")

    assert {s.type for s in manifest.samples} == {"image/plain", "image/frames"}
    paths_by_checksum: dict[str, set[str]] = {}
    for sample in manifest.samples:
        paths_by_checksum.setdefault(sample.checksum, set()).add(sample.path)
    assert all(len(p) == 1 for p in paths_by_checksum.values())
    written = sorted(p.name for p in (tmp_path / "d" / "files").iterdir())
    assert len(written) == 4
    for sample in manifest.samples:
        assert (tmp_path / "d" / sample.path).read_bytes().startswith(b"contents of img")


def test_two_types_over_three_files_are_six_rows_over_three_checksums(catalog, files):
    both(catalog, files(3))
    with catalog.engine.connect() as conn:
        rows = conn.execute(select(func.count()).select_from(t.sample)).scalar_one()
        checksums = conn.execute(select(func.count(t.sample.c.checksum.distinct()))).scalar_one()
    assert (rows, checksums) == (6, 3)
