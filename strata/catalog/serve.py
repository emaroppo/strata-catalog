"""Running the blob server from an environment.

The process that owns a deployment, as opposed to :mod:`server`, which owns
an app. Where the catalog is comes from a config file in the format the CLI
reads, named by ``$STRATA_CONFIG``, and the file's default is the catalog
served. The secret URLs are signed with, and the bucket's credentials, come
from the environment.

It refuses to start rather than start degraded. A server missing its signing
secret would serve the corpus to anyone who guessed a checksum, and a server
missing its index would answer 404 to every request that ever mattered —
both look like working processes from the outside.
"""

import os


class ConfigError(Exception):
    """A setting the server cannot start without."""


def _required(name: str, why: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"${name} is not set. {why}")
    return value


def build():
    """The app, from the environment. Raises if anything essential is absent."""
    from .config import CatalogConfigError, CatalogMissing, host_catalog, open_catalog
    from .server import create_app

    # Opened by the same code the CLI uses, from the same format of file, so
    # the two cannot disagree about where a catalog is or how to read it.
    try:
        name, config = host_catalog()
    except CatalogConfigError as e:
        raise ConfigError(str(e)) from None
    if not config.blob_secret:
        raise ConfigError(
            "$STRATA_BLOB_SECRET is not set. URLs are signed with it, and it must "
            "match what the labeller signs with, or nothing a reviewer opens will load."
        )
    try:
        catalog = open_catalog(config)
    except CatalogMissing as e:
        # Otherwise it would serve an empty catalog and answer 404 to every
        # request, which reads as "the catalog is empty"
        raise ConfigError(str(e)) from None

    # Comma-separated, and everything by default. Label Studio marks images
    # crossorigin and fetches documents with XHR, so without a matching
    # header the browser discards a response it already received in full.
    origins = tuple(
        o.strip() for o in os.environ.get("STRATA_SERVE_ORIGINS", "*").split(",") if o.strip()
    )
    return create_app(catalog, config.blob_secret, allow_origins=origins, name=name)


def main() -> None:
    """Entry point. Serves until stopped."""
    import sys

    import uvicorn

    try:
        app = build()
    except ConfigError as e:
        # stderr and a non-zero exit, so a supervisor reports a failed start
        # rather than restarting something that will never work
        print(f"strata-blobs: {e}", file=sys.stderr)
        raise SystemExit(2) from None

    uvicorn.run(
        app,
        host=os.environ.get("STRATA_SERVE_HOST", "0.0.0.0"),  # noqa: S104
        port=int(os.environ.get("STRATA_SERVE_PORT", "8081")),
    )
