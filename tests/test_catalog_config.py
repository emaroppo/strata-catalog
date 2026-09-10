"""Which catalog a host uses, read from config.toml, and opening it.

Moved here from the labeller along with the parsing, so the blob server and
the modelling host can read the same description. Everything below is about
one failure: sample ids mean nothing outside the catalog that issued them,
so a process that opens the wrong one reports real numbers about the wrong
data, and nothing raises. So a name that resolves to nothing is refused
rather than guessed, and the environment cannot move a host to another
catalog behind the file's back.
"""

import pytest

from strata.catalog import LocalBackend
from strata.catalog.config import (
    DEFAULT_CATALOG,
    CatalogConfig,
    CatalogConfigError,
    CatalogMissing,
    blobs_for,
    load_catalogs,
    open_catalog,
)


def _write(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in (
        "STRATA_CATALOG_URL",
        "STRATA_S3_ENDPOINT",
        "STRATA_S3_BUCKET",
        "STRATA_S3_ACCESS_KEY",
        "STRATA_S3_SECRET_KEY",
        "STRATA_BLOB_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)


# ----------------------------------------------------------------------
# A host with one catalog
# ----------------------------------------------------------------------


def test_a_flat_catalog(tmp_path):
    catalogs = load_catalogs(_write(tmp_path, '[catalog]\nroot = "somewhere"\n'))
    assert catalogs.named().root == "somewhere"
    assert catalogs.names() == [DEFAULT_CATALOG]


def test_no_catalog_section_at_all(tmp_path):
    catalogs = load_catalogs(_write(tmp_path, '[label_studio]\nurl = "x"\n'))
    assert catalogs.named().root == "catalog"


def test_no_file_at_all(tmp_path):
    assert load_catalogs(tmp_path / "absent.toml").named().root == "catalog"


# ----------------------------------------------------------------------
# Named catalogs
# ----------------------------------------------------------------------


def test_named_catalogs_are_addressable(tmp_path):
    catalogs = load_catalogs(_write(tmp_path, """
[catalog.images]
root = "images"

[catalog.text]
root = "text"
"""))
    assert catalogs.names() == ["images", "text"]
    assert catalogs.named("images").root == "images"
    assert catalogs.named("text").root == "text"


def test_shared_settings_are_stated_once(tmp_path):
    """Flat keys are shared, tables layer on top.

    Two catalogs on one endpoint is the ordinary case, and repeating the
    endpoint in every table is how one of them ends up subtly different.
    """
    catalogs = load_catalogs(_write(tmp_path, """
[catalog]
s3_endpoint = "http://garage:3900"
s3_bucket = "shared"

[catalog.images]
root = "images"

[catalog.text]
root = "text"
s3_bucket = "text-only"
"""))
    assert catalogs.named("images").s3_endpoint == "http://garage:3900"
    assert catalogs.named("text").s3_endpoint == "http://garage:3900"
    # and a table overrides what it names
    assert catalogs.named("images").s3_bucket == "shared"
    assert catalogs.named("text").s3_bucket == "text-only"


def test_a_single_named_catalog_needs_no_default(tmp_path):
    catalogs = load_catalogs(_write(tmp_path, '[catalog.only]\nroot = "one"\n'))
    assert catalogs.default_name == "only"
    assert catalogs.named().root == "one"


def test_an_explicit_default_is_honoured(tmp_path):
    catalogs = load_catalogs(_write(tmp_path, """
[catalog]
default = "text"

[catalog.images]
root = "images"

[catalog.text]
root = "text"
"""))
    assert catalogs.named().root == "text"


# ----------------------------------------------------------------------
# What is refused
# ----------------------------------------------------------------------


def test_an_unknown_name_lists_the_ones_that_exist(tmp_path):
    catalogs = load_catalogs(_write(tmp_path, """
[catalog.images]
root = "images"

[catalog.text]
root = "text"
"""))
    with pytest.raises(CatalogConfigError, match="images, text"):
        catalogs.named("satellite")


def test_an_unknown_name_does_not_fall_back(tmp_path):
    """The whole point. A typo must not silently open another corpus."""
    catalogs = load_catalogs(_write(tmp_path, '[catalog.images]\nroot = "images"\n'))
    with pytest.raises(CatalogConfigError):
        catalogs.named("imagse")


def test_several_catalogs_and_no_default_is_ambiguous(tmp_path):
    catalogs = load_catalogs(_write(tmp_path, """
[catalog.images]
root = "images"

[catalog.text]
root = "text"
"""))
    # Not at load time — a project or --catalog naming one settles it, and
    # only asking for "the default" is genuinely ambiguous
    with pytest.raises(CatalogConfigError, match="several catalogs"):
        catalogs.named()
    assert catalogs.named("text").root == "text"


def test_a_default_naming_nothing_is_refused(tmp_path):
    with pytest.raises(CatalogConfigError, match="names no catalog"):
        load_catalogs(_write(tmp_path, """
[catalog]
default = "missing"

[catalog.images]
root = "images"
"""))


def test_an_unknown_key_says_where_it_is(tmp_path):
    with pytest.raises(CatalogConfigError, match="catalog.images"):
        load_catalogs(_write(tmp_path, '[catalog.images]\nrooot = "typo"\n'))


def test_a_credential_in_the_file_is_refused(tmp_path):
    """The file is copied between machines; a secret in it goes with every copy."""
    with pytest.raises(CatalogConfigError, match=r"\$STRATA_S3_SECRET_KEY"):
        load_catalogs(_write(tmp_path, '[catalog]\ns3_secret_key = "oops"\n'))


def test_blobs_prefix_says_where_it_went(tmp_path):
    with pytest.raises(CatalogConfigError, match=r"\[label_studio\]"):
        load_catalogs(_write(tmp_path, '[catalog]\nblobs_prefix = "blobs"\n'))


# ----------------------------------------------------------------------
# The environment
# ----------------------------------------------------------------------


def test_credentials_reach_every_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("STRATA_S3_ACCESS_KEY", "key")
    monkeypatch.setenv("STRATA_BLOB_SECRET", "secret")
    catalogs = load_catalogs(_write(tmp_path, """
[catalog.images]
root = "images"

[catalog.text]
root = "text"
"""))
    for name in ("images", "text"):
        assert catalogs.named(name).s3_access_key == "key"
        assert catalogs.named(name).blob_secret == "secret"


def test_the_environment_cannot_move_a_host_to_another_catalog(tmp_path, monkeypatch):
    """Where the catalog is lives in the file alone.

    Otherwise a switch made in the file is quietly undone by a variable
    exported in some shell, and the host keeps reading the old catalog.
    """
    monkeypatch.setenv("STRATA_CATALOG_URL", "postgresql://elsewhere/db")
    monkeypatch.setenv("STRATA_S3_ENDPOINT", "http://elsewhere:3900")
    monkeypatch.setenv("STRATA_S3_BUCKET", "elsewhere")
    config = load_catalogs(_write(tmp_path, """
[catalog]
url = "postgresql://here/db"
s3_endpoint = "http://here:3900"
s3_bucket = "here"
""")).named()
    assert (config.url, config.s3_endpoint, config.s3_bucket) == (
        "postgresql://here/db",
        "http://here:3900",
        "here",
    )


def test_a_bucket_stays_with_its_catalog(tmp_path, monkeypatch):
    """An exported bucket used to land on every catalog, and put them all in one."""
    monkeypatch.setenv("STRATA_S3_BUCKET", "strata")
    catalogs = load_catalogs(_write(tmp_path, """
[catalog]
s3_endpoint = "http://garage:3900"
s3_bucket = "strata"

[catalog.images]

[catalog.text]
s3_bucket = "text"
"""))
    assert catalogs.named("text").s3_bucket == "text"


def test_credentials_stay_out_of_what_gets_printed(monkeypatch, tmp_path):
    monkeypatch.setenv("STRATA_S3_SECRET_KEY", "hunter2")
    config = load_catalogs(tmp_path / "absent.toml").named()
    assert config.s3_secret_key == "hunter2"
    assert "hunter2" not in repr(config)


# ----------------------------------------------------------------------
# Opening one
# ----------------------------------------------------------------------


def test_a_configured_index_wins_over_a_local_file(tmp_path):
    """A stale catalog.db must not shadow the index that is configured.

    Five commands once opened SQLite whenever the file existed, regardless
    of the configured index. After the index moved to Postgres they carried
    on reporting counts from a file nobody was writing to any more.
    """
    root = tmp_path / "catalog"
    root.mkdir()
    (root / "catalog.db").write_bytes(b"")
    elsewhere = tmp_path / "elsewhere.db"

    catalog = open_catalog(CatalogConfig(root=str(root), url=f"sqlite:///{elsewhere}"))
    assert str(elsewhere) in str(catalog.engine.url)


def test_a_local_catalog_is_made_only_when_asked(tmp_path):
    config = CatalogConfig(root=str(tmp_path / "catalog"))
    with pytest.raises(CatalogMissing, match="No catalog at"):
        open_catalog(config)
    assert open_catalog(config, create=True) is not None
    # and once made, it is simply there
    assert open_catalog(config) is not None


def test_local_blobs_are_under_the_root_unless_told_otherwise(tmp_path):
    config = CatalogConfig(root=str(tmp_path / "catalog"))
    assert blobs_for(config).root == tmp_path / "catalog" / "blobs"
    assert blobs_for(config, local=tmp_path / "shared").root == tmp_path / "shared"
    assert isinstance(blobs_for(config), LocalBackend)


def test_a_bucket_is_opened_with_the_catalogs_own_settings():
    pytest.importorskip("boto3")
    backend = blobs_for(
        CatalogConfig(
            s3_endpoint="http://garage:3900",
            s3_bucket="text",
            s3_access_key="key",
            s3_secret_key="secret",
        )
    )
    assert backend.bucket == "text"
