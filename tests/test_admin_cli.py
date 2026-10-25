"""``strata-catalog``: each command reaches its operation and renders what came back."""

import json

import pytest

from strata.catalog import EVERYTHING, Catalog
from strata.catalog.cli import main
from strata.contracts import Choices, ClassificationSchema


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


def test_without_config_the_environment_names_the_file(stocked, config, monkeypatch, capsys):
    # ADR 0019: --config, else $STRATA_CONFIG, else ./config.toml
    monkeypatch.setenv("STRATA_CONFIG", str(config))
    code, out = run(capsys, "--json", "list")
    assert code == 0
    [entry] = json.loads(out)
    assert entry["name"] == "default" and entry["identity"] == stocked.id


def test_the_environment_naming_a_missing_file_is_refused(tmp_path, monkeypatch, capsys):
    absent = tmp_path / "absent.toml"
    monkeypatch.setenv("STRATA_CONFIG", str(absent))
    assert main(["list"]) == 1
    captured = capsys.readouterr()
    assert "STRATA_CONFIG" in captured.err and str(absent) in captured.err
    assert captured.out == ""


def test_a_credential_in_the_file_is_refused(tmp_path, capsys):
    config = tmp_path / "config.toml"
    config.write_text('[catalog]\ns3_secret_key = "oops"\n')
    code = main(["--config", str(config), "list"])
    assert code == 1
    assert "STRATA_S3_SECRET_KEY" in capsys.readouterr().err


def test_types_read_what_is_installed_and_what_each_requires(capsys):
    code, out = run(capsys, "types")
    assert code == 0 and "image" in out and "frames" in out
    code, out = run(capsys, "--json", "types")
    entries = {e["name"]: e for e in json.loads(out)}
    assert set(entries) >= {"image", "text", "frames"}
    # What a corpus of each must arrive with, and how its bytes are kept
    assert entries["frames"]["requires"] == ["video"]
    assert entries["image"]["requires"] == []
    assert entries["text"]["canonical"] and not entries["image"]["canonical"]


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
    # A refusal is one line on stderr and nothing on stdout. docs/adr/0030
    captured = capsys.readouterr()
    assert "No catalog at" in captured.err and str(config) in captured.err
    assert captured.out == ""


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
    Catalog.local(tmp_path / "fresh")

    code, out = run(capsys, "--config", str(config), "--json", "probe")
    record = json.loads(out)
    assert code == 0
    assert record["index"]["samples"] == 0
    assert record["blobs"]["ok"] and record["blobs"]["bytes"] == 16384
    assert record["blobs"]["sample"] is None and "no samples" in record["blobs"]["note"]


def test_probe_reports_a_catalog_that_is_not_there_and_makes_none(tmp_path, capsys):
    # A command that only looks must not leave an empty catalog behind. docs/adr/0018
    root = tmp_path / "nowhere"
    config = tmp_path / "config.toml"
    config.write_text(f'[catalog]\nroot = "{root}"\n')

    code, out = run(capsys, "--config", str(config), "--json", "probe")
    record = json.loads(out)
    assert code == 1
    assert not record["index"]["reachable"] and "No catalog at" in record["index"]["error"]
    assert record["blobs"] is None
    assert not (root / "catalog.db").exists() and not root.exists()
    code, out = run(capsys, "--config", str(config), "probe")
    assert "unreachable — No catalog at" in out


def test_remove_tombstones_what_a_selection_names(stocked, config, capsys):
    label_set = stocked.label_sets.get("presence")[0]
    # Two of the six are bad pictures, marked as such in their metadata
    bad = [s.id for s in stocked.samples.unlabelled(label_set, EVERYTHING)]
    with stocked.engine.begin() as conn:
        for sample_id in bad:
            stocked.samples.describe(conn, sample_id, metadata={"quality": "bad"})

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


def test_lists_put_show_and_ls(stocked, config, capsys, tmp_path):
    checksums = sorted(s.checksum for s in stocked.samples.matching("*"))[:2]
    listed = tmp_path / "dups.txt"
    listed.write_text("\n".join(checksums) + "\n")
    code, out = run(capsys, "--config", str(config), "lists", "put", "dups", str(listed))
    assert code == 0 and "dups@1: 2 checksum(s), stored" in out
    code, out = run(capsys, "--config", str(config), "lists", "put", "dups", str(listed))
    assert "dups@1" in out and "already held" in out
    _code, out = run(capsys, "--config", str(config), "lists", "show", "dups@1")
    assert out.split() == checksums
    _code, out = run(capsys, "--config", str(config), "lists", "ls")
    assert out.startswith("dups@1") and "untyped" in out
    code = main(["--config", str(config), "lists", "show", "dups"])
    assert code == 1 and "names no version" in capsys.readouterr().err


def test_lists_put_records_the_type_its_members_are(stocked, config, capsys, tmp_path):
    checksums = sorted(s.checksum for s in stocked.samples.matching("*"))[:2]
    listed = tmp_path / "dups.txt"
    listed.write_text("\n".join(checksums) + "\n")
    put = ["--config", str(config), "lists", "put", "dups", str(listed)]
    run(capsys, *put)
    # The same set with its type stated says more than the untyped version
    code, out = run(capsys, *put, "--type", "image/plain")
    assert code == 0 and "dups@2: 2 checksum(s) of image/plain, stored" in out
    _code, out = run(capsys, "--config", str(config), "lists", "ls")
    assert "image/plain" in out.splitlines()[1]
    code = main(["--config", str(config), "lists", "put", "x", str(listed), "--type", "image"])
    assert code == 1 and "not a sample type" in capsys.readouterr().err


def test_dataset_verify_runs_the_spec_again(stocked, config, capsys):
    label_set, _ = stocked.label_sets.get("presence")
    stocked.create_dataset("d", label_set, collections=["photos"])
    code, out = run(capsys, "--config", str(config), "dataset", "verify", "d@1")
    assert code == 0 and out.rstrip().endswith("Holds.")
    stocked.create_dataset("named", label_set, sample_ids=[1, 2])
    code = main(["--config", str(config), "dataset", "verify", "named@1"])
    assert code == 1 and "records no selection spec" in capsys.readouterr().err
