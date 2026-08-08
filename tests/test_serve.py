"""Starting the server from an environment.

Almost entirely about refusing to start. A blob server that comes up without
a signing secret, or pointed at nothing, looks healthy from the outside and
is not — so the failures worth testing are the ones that would otherwise be
silent.
"""

import pytest

from strata.catalog.serve import ConfigError, build

ESSENTIAL = {
    "STRATA_CATALOG_URL": "sqlite:///:memory:",
    "STRATA_BLOB_SECRET": "shared with the labeller",
    "STRATA_BLOBS_ROOT": "/tmp/blobs",
}


@pytest.fixture
def env(monkeypatch):
    def _set(**overrides):
        for name in (*ESSENTIAL, "STRATA_S3_ENDPOINT", "STRATA_S3_BUCKET"):
            monkeypatch.delenv(name, raising=False)
        for name, value in {**ESSENTIAL, **overrides}.items():
            if value is None:
                continue
            monkeypatch.setenv(name, value)

    return _set


def test_it_builds_with_what_it_needs(env):
    env()
    assert build() is not None


def test_no_secret_is_a_refusal(env):
    env(STRATA_BLOB_SECRET=None)
    # Starting anyway would serve the corpus to anyone who guessed a
    # checksum, and the process would look perfectly healthy
    with pytest.raises(ConfigError, match="STRATA_BLOB_SECRET"):
        build()


def test_no_index_is_a_refusal(env):
    env(STRATA_CATALOG_URL=None)
    with pytest.raises(ConfigError, match="STRATA_CATALOG_URL"):
        build()


def test_an_endpoint_without_a_bucket_is_a_refusal(env):
    env(STRATA_S3_ENDPOINT="http://garage:3900")
    with pytest.raises(ConfigError, match="STRATA_S3_BUCKET"):
        build()


def test_no_endpoint_needs_somewhere_to_read_files(env):
    env(STRATA_BLOBS_ROOT=None)
    # Otherwise it would serve an empty directory and answer 404 to
    # everything, which reads as "the catalog is empty"
    with pytest.raises(ConfigError, match="STRATA_BLOBS_ROOT"):
        build()
