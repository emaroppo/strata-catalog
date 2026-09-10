"""Starting the server from its config file and environment.

Almost entirely about refusing to start. A blob server that comes up without
a signing secret, or pointed at nothing, looks healthy from the outside and
is not — so the failures worth testing are the ones that would otherwise be
silent.
"""

import pytest
from fastapi.testclient import TestClient

from strata.catalog import Catalog
from strata.catalog.serve import ConfigError, build

SECRET = "shared with the labeller"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A config naming one real local catalog, and the secret; overridable."""
    Catalog.local(tmp_path / "catalog")
    config = tmp_path / "config.toml"
    config.write_text(f'[catalog]\nroot = "{tmp_path / "catalog"}"\n')
    for name in ("STRATA_CATALOG_URL", "STRATA_S3_ENDPOINT", "STRATA_S3_BUCKET"):
        monkeypatch.delenv(name, raising=False)

    def _set(**overrides):
        values = {"STRATA_CONFIG": str(config), "STRATA_BLOB_SECRET": SECRET, **overrides}
        for name, value in values.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        return config

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


def test_no_config_is_a_refusal(env):
    env(STRATA_CONFIG=None)
    with pytest.raises(ConfigError, match="STRATA_CONFIG"):
        build()


def test_a_config_that_is_not_there_is_a_refusal(env, tmp_path):
    env(STRATA_CONFIG=str(tmp_path / "absent.toml"))
    with pytest.raises(ConfigError, match="does not exist"):
        build()


def test_a_catalog_that_does_not_exist_is_a_refusal(env, tmp_path):
    config = env()
    config.write_text(f'[catalog]\nroot = "{tmp_path / "nowhere"}"\n')
    # Otherwise it would serve an empty catalog and answer 404 to everything,
    # which reads as "the catalog is empty"
    with pytest.raises(ConfigError, match="No catalog at"):
        build()


def test_the_files_default_is_the_catalog_served(env, tmp_path):
    """Switching a server to another catalog is changing the default and restarting."""
    other = Catalog.local(tmp_path / "other")
    config = env()
    config.write_text(f"""
[catalog]
default = "other"

[catalog.main]
root = "{tmp_path / "catalog"}"

[catalog.other]
root = "{tmp_path / "other"}"
""")
    served = TestClient(build()).get("/healthz").json()["catalog"]
    assert served == {"name": "other", "id": other.id}


def test_the_environment_does_not_choose_the_catalog(env, monkeypatch, tmp_path):
    env()
    monkeypatch.setenv("STRATA_CATALOG_URL", f"sqlite:///{tmp_path / 'elsewhere.db'}")
    served = TestClient(build()).get("/healthz").json()["catalog"]
    assert served["id"] == Catalog.local(tmp_path / "catalog").id
