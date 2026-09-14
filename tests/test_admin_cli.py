"""``strata-catalog``: each command reaches its operation and renders what came back."""

import json

import pytest

from strata.catalog import EVERYTHING, Catalog
from strata.catalog.cli import main
from strata.labels import Choices, ClassificationSchema


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[catalog]\nroot = "{tmp_path / "catalog"}"\n')
    return path


@pytest.fixture
def stocked(tmp_path, config):
    catalog = Catalog.local(tmp_path / "catalog")
    raw = tmp_path / "raw"
    raw.mkdir()
    paths = []
    for i in range(6):
        path = raw / f"img{i}.jpg"
        path.write_bytes(f"image {i}".encode())
        paths.append(path)
    ids = catalog.ingest(paths, media="image", collections=["photos"])
    label_set = catalog.label_sets.create("presence", ClassificationSchema(classes=["cat", "dog"]))
    catalog.annotations.annotate_many(label_set, [(i, Choices(values=["cat"])) for i in ids[:4]])
    return catalog


def run(capsys, *argv) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


# ----------------------------------------------------------------------


def test_list_names_each_catalog_and_its_identity(stocked, config, capsys):
    code, out = run(capsys, "--config", str(config), "list")
    assert code == 0
    assert "default (default)" in out and stocked.id in out


def test_list_as_json_is_the_record(stocked, config, capsys):
    _code, out = run(capsys, "--config", str(config), "--json", "list")
    [entry] = json.loads(out)
    assert entry["name"] == "default" and entry["identity"] == stocked.id


def test_a_credential_in_the_file_is_refused(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[catalog]\ns3_secret_key = "oops"\n')
    code = main(["--config", str(config), "list"])
    assert code == 1
    assert "STRATA_S3_SECRET_KEY" in capsys.readouterr().err


def test_types_and_preparers_read_what_is_installed(capsys):
    code, out = run(capsys, "types")
    assert code == 0 and "image" in out and "frames" in out
    code, out = run(capsys, "--json", "types")
    assert {e["name"] for e in json.loads(out)} >= {"image", "text", "frames"}
    code, _ = run(capsys, "preparers")
    assert code == 0


def test_stats_counts_samples_collections_and_classes(stocked, config, capsys):
    code, out = run(capsys, "--config", str(config), "--json", "stats")
    assert code == 0
    record = json.loads(out)
    assert record["samples"] == 6
    assert record["collections"] == {"photos": 6}
    [label_set] = record["label_sets"]
    assert (label_set["annotated"], label_set["awaiting"]) == (4, 2)
    assert label_set["classes"] == {"cat": 4, "dog": 0}
    # Rendered for a person, the same numbers
    code, out = run(capsys, "--config", str(config), "stats")
    assert "6 sample(s)" in out and "4 annotated, 2 awaiting review" in out


def test_stats_refuses_a_catalog_that_is_not_there(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(f'[catalog]\nroot = "{tmp_path / "nowhere"}"\n')
    assert main(["--config", str(config), "stats"]) == 1
    assert "No catalog at" in capsys.readouterr().err


def test_probe_reads_a_real_sample_back(stocked, config, capsys):
    import hashlib

    code, out = run(capsys, "--config", str(config), "--json", "probe")
    assert code == 0
    record = json.loads(out)
    assert record["index"]["reachable"] and record["index"]["samples"] == 6
    # The first sample the catalog holds, through the same read every
    # consumer makes, hashed against what the index recorded for it
    assert record["blobs"]["ok"] and record["blobs"]["bytes"] == len(b"image 0")
    assert record["blobs"]["sample"] == hashlib.sha256(b"image 0").hexdigest()
    code, out = run(capsys, "--config", str(config), "probe")
    assert "reads back as recorded" in out


def test_probe_notices_a_store_that_does_not_hold_the_samples(stocked, config, capsys):
    # The index is fine; the bytes under the root are not what it describes
    files = (p for p in stocked.blobs.root.rglob("*") if p.is_file())
    [blob] = [p for p in files if p.read_bytes() == b"image 0"]
    blob.write_bytes(b"not image 0 at all")

    code, out = run(capsys, "--config", str(config), "--json", "probe")
    record = json.loads(out)
    assert code == 1
    assert record["index"]["reachable"]
    assert not record["blobs"]["ok"] and "the index says" in record["blobs"]["error"]


def test_probe_on_an_empty_catalog_falls_back_to_a_round_trip(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text(f'[catalog]\nroot = "{tmp_path / "fresh"}"\n')

    code, out = run(capsys, "--config", str(config), "--json", "probe")
    record = json.loads(out)
    assert code == 0
    assert record["index"]["samples"] == 0
    assert record["blobs"]["ok"] and record["blobs"]["bytes"] == 16384
    assert record["blobs"]["sample"] is None and "no samples" in record["blobs"]["note"]


def test_remove_tombstones_what_a_selection_names(stocked, config, capsys):
    label_set = stocked.label_sets.get("presence")[0]
    # Two of the six are bad pictures, marked as such in their metadata
    bad = [s.id for s in stocked.samples.unlabelled(label_set, EVERYTHING)]
    with stocked.engine.begin() as conn:
        for sample_id in bad:
            stocked.samples.describe(
                conn, sample_id, subtype="plain", metadata={"quality": "bad"}
            )

    code, out = run(
        capsys, "--config", str(config), "remove", "--where", "quality=bad", "--dry-run"
    )
    assert code == 0 and "2 live sample(s) match" in out and "nothing removed" in out.lower()
    assert len(stocked.samples.unlabelled(label_set, EVERYTHING)) == 2

    code, out = run(capsys, "--config", str(config), "remove", "--where", "quality=bad")
    assert code == 0 and "2 removed" in out
    # Gone from every reader; the answered four are untouched
    assert stocked.samples.unlabelled(label_set, EVERYTHING) == []
    assert len(stocked.samples.labelled(label_set, EVERYTHING)) == 4
    # And a second run finds nothing live to match
    code, out = run(capsys, "--config", str(config), "remove", "--where", "quality=bad")
    assert "0 live sample(s) match" in out


def test_remove_refuses_to_mean_the_whole_catalog(config, capsys, stocked):
    code = main(["--config", str(config), "remove"])
    assert code == 1
    assert "needs a selection" in capsys.readouterr().err


def test_copy_moves_the_index_and_keeps_ids(stocked, config, tmp_path, capsys):
    target = f"sqlite:///{tmp_path / 'copy.db'}"
    code, out = run(capsys, "--config", str(config), "copy", "--to", target)
    assert code == 0 and "row(s) copied" in out
    copied = Catalog.connect(target, stocked.blobs)
    assert copied.id == stocked.id
    label_set = copied.label_sets.get("presence")[0]
    assert len(copied.samples.labelled(label_set, EVERYTHING)) == 4


def test_merge_reports_before_it_writes(stocked, config, tmp_path, capsys):
    other_url = f"sqlite:///{tmp_path / 'other.db'}"
    other = Catalog.create(other_url, stocked.blobs)
    from strata.catalog import copy_index

    copy_index(stocked, other)
    label_set = other.label_sets.get("presence")[0]
    pool = other.samples.unlabelled(label_set, EVERYTHING)
    other.annotations.annotate(pool[0].id, label_set, Choices(values=["dog"]))

    code, out = run(capsys, "--config", str(config), "merge", "--from", other_url)
    assert code == 0 and "Nothing was written" in out
    assert len(stocked.samples.labelled(stocked.label_sets.get("presence")[0], EVERYTHING)) == 4

    code, out = run(
        capsys, "--config", str(config), "--json", "merge", "--from", other_url, "--apply"
    )
    assert code == 0
    assert json.loads(out)["copied"] == 1
    assert len(stocked.samples.labelled(stocked.label_sets.get("presence")[0], EVERYTHING)) == 5


def test_repack_needs_a_bucket(stocked, config, capsys):
    assert main(["--config", str(config), "repack", "--dry-run"]) == 1
    assert "No object storage configured" in capsys.readouterr().err
