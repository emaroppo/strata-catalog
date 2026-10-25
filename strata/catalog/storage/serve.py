"""Running the blob server from an environment: the config file says which catalog,
the environment holds the secret, and it refuses to start degraded. See ``docs/adr/0019``.
"""

import os

from strata.common.service import serve

from ..config import (
    DEFAULT_CATALOG,
    CatalogConfigError,
    CatalogMissing,
    host_catalog,
    open_catalog,
    secret_variables,
)
from .server import create_app


class ConfigError(Exception):
    """A setting the server cannot start without."""


def build():
    """The app, from the environment. Raises if anything essential is absent."""
    # Opened by the same code the CLI uses. docs/adr/0019
    try:
        name, config = host_catalog()
    except CatalogConfigError as e:
        raise ConfigError(str(e)) from None
    if not config.blob_secret:
        # The flat [catalog] goes by the default name but reads no suffix
        own, *shared = secret_variables(
            "STRATA_BLOB_SECRET", "" if name == DEFAULT_CATALOG else name
        )
        also = f", and neither is ${shared[0]}" if shared else ""
        raise ConfigError(
            f"${own} is not set{also}. URLs are signed with it, and it must match "
            f"what the labeller signs with, or nothing a reviewer opens will load."
        )
    try:
        catalog = open_catalog(config)
    except CatalogMissing as e:
        # Otherwise it would serve an empty catalog. docs/adr/0019
        raise ConfigError(str(e)) from None

    origins = tuple(
        o.strip() for o in os.environ.get("STRATA_SERVE_ORIGINS", "*").split(",") if o.strip()
    )
    return create_app(catalog, config.blob_secret, allow_origins=origins, name=name)


def main() -> None:
    """Entry point. Serves until stopped."""
    serve(build, prog="strata-blobs", port=8081, error=ConfigError)
