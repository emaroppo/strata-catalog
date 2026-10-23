"""``strata-catalog ingest``, ``raw ls``, ``runs`` and ``verify``. See docs/adr/0045."""

import json

import pytest
from sqlalchemy import delete, update

from strata.catalog import Catalog
from strata.catalog.cli import main
from strata.catalog.index import tables as t


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[catalog]\nroot = "{tmp_path / "catalog"}"\n')
    return path


@pytest.fixture
def maildir(tmp_path):
    root = tmp_path / "maildir"
    for path, body in {"a/inbox/6.": b"one", "a/sent/1.": b"two", "b/inbox/2.": b"three"}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(body)
    return root


def run(capsys, config, *argv) -> tuple[int, str, str]:
    code = main(["--config", str(config), *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _ingested(capsys, config, maildir, *extra) -> str:
    code, out, _ = run(
        capsys, config, "--json", "ingest", str(maildir), "--collection", "enron/maildir", *extra
    )
    assert code == 0
    return json.loads(out)["run_id"]


def test_ingest_makes_the_catalog_and_reports_the_run(capsys, config, maildir):
    code, out, _ = run(capsys, config, "ingest", str(maildir), "--collection", "enron/maildir")
    assert code == 0
    assert "3 file(s): 3 added" in out


def test_a_file_list_limits_the_ingest(capsys, config, maildir, tmp_path):
    listed = tmp_path / "list.txt"
    listed.write_text("a/inbox/6.\nb/inbox/2.\n")
    _ingested(capsys, config, maildir, "--files", str(listed))
    _, out, _ = run(capsys, config, "raw", "ls", "enron/maildir")
    assert out.splitlines() == ["enron/maildir/a/inbox/6.", "enron/maildir/b/inbox/2."]


def test_a_refused_ingest_says_why_and_fails(capsys, config, maildir, tmp_path):
    listed = tmp_path / "list.txt"
    listed.write_text("a/inbox/404.\n")
    code, _, err = run(
        capsys, config, "ingest", str(maildir), "--collection", "enron/maildir",
        "--files", str(listed),
    )  # fmt: skip
    assert code == 1
    assert "a/inbox/404." in err


def test_raw_ls_without_a_collection_counts_each(capsys, config, maildir):
    _ingested(capsys, config, maildir)
    _, out, _ = run(capsys, config, "raw", "ls")
    assert out.split() == ["enron/maildir", "3"]


def test_runs_lists_what_wrote(capsys, config, maildir):
    run_id = _ingested(capsys, config, maildir)
    _, out, _ = run(capsys, config, "--json", "runs")
    [record] = json.loads(out)
    assert (record["id"], record["kind"]) == (run_id, "ingest")


def test_an_untouched_run_verifies(capsys, config, maildir):
    run_id = _ingested(capsys, config, maildir)
    code, out, _ = run(capsys, config, "verify", "--run", run_id, "--deep")
    assert code == 0, out
    assert out.rstrip().endswith("Holds.")


def _catalog(tmp_path) -> Catalog:
    return Catalog.local(tmp_path / "catalog")


def test_a_lost_raw_row_fails_verification(capsys, config, maildir, tmp_path):
    run_id = _ingested(capsys, config, maildir)
    with _catalog(tmp_path).engine.begin() as conn:
        conn.execute(delete(t.raw).where(t.raw.c.path == "a/sent/1."))
    code, out, _ = run(capsys, config, "verify", "--run", run_id)
    assert code == 1
    assert "BAD  counts" in out and "BAD  present" in out


def test_an_altered_input_list_fails_verification(capsys, config, maildir, tmp_path):
    run_id = _ingested(capsys, config, maildir)
    with _catalog(tmp_path).engine.begin() as conn:
        conn.execute(delete(t.run_input).where(t.run_input.c.run_id == run_id))
    code, out, _ = run(capsys, config, "verify", "--run", run_id)
    assert code == 1
    assert "BAD  files" in out


def test_a_blob_that_no_longer_hashes_fails_deep_verification(capsys, config, maildir, tmp_path):
    run_id = _ingested(capsys, config, maildir)
    catalog = _catalog(tmp_path)
    [row] = [r for r in catalog.raw.names("enron/maildir") if r.path == "a/inbox/6."]
    # Linked, not copied, so the blob is the file: break the link first
    blob = catalog.blobs.path_for(row.location)
    blob.unlink()
    blob.write_bytes(b"not one")
    with catalog.engine.begin() as conn:
        conn.execute(
            update(t.raw).where(t.raw.c.path == "a/inbox/6.").values(length=len(b"not one"))
        )
    code, out, _ = run(capsys, config, "verify", "--run", run_id, "--deep")
    assert code == 1
    assert "BAD  deep" in out


def test_verifying_a_run_nobody_recorded_is_refused(capsys, config, maildir):
    _ingested(capsys, config, maildir)
    code, _, err = run(capsys, config, "verify", "--run", "nope")
    assert code == 1
    assert "No run 'nope'" in err
